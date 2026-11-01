from __future__ import annotations

import datetime as dt
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[str], None]


class BackupError(RuntimeError):
    pass


@dataclass
class BackupResult:
    path: Path
    bytes_written: int
    database: str


def safe_filename_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned[:80] or "database"


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _quote_identifier(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else "NULL"
    if isinstance(value, dt.datetime):
        return "'" + value.isoformat(sep=" ") + "'"
    if isinstance(value, (dt.date, dt.time)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "_binary 0x" + bytes(value).hex()

    text = str(value)
    text = (
        text.replace("\\", "\\\\")
        .replace("\x00", "\\0")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x1a", "\\Z")
        .replace("'", "\\'")
    )
    return f"'{text}'"


def _write_text(handle, text: str) -> None:
    handle.write(text)
    if not text.endswith("\n"):
        handle.write("\n")


def _mysql_connect(config: dict, database: str | None = None):
    try:
        import pymysql
    except ImportError as exc:
        raise BackupError("缺少 MySQL 驱动，请重新安装程序。") from exc

    password = str(config.get("mysql_password", ""))
    try:
        password.encode("latin1")
    except UnicodeEncodeError as exc:
        raise BackupError(
            "当前 MySQL 连接组件不支持密码中的中文等非拉丁字符。"
            "请为该备份账号设置仅包含英文、数字和常用符号的密码。"
        ) from exc

    try:
        return pymysql.connect(
            host=str(config.get("mysql_host", "127.0.0.1")).strip(),
            port=int(config.get("mysql_port", 3306)),
            user=str(config.get("mysql_user", "root")).strip(),
            password=password,
            database=database,
            charset="utf8mb4",
            connect_timeout=10,
            read_timeout=86400,
            write_timeout=86400,
            autocommit=False,
        )
    except Exception as exc:
        raise BackupError(f"MySQL 连接失败：{exc}") from exc


def test_mysql(config: dict) -> str:
    connection = _mysql_connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT VERSION()")
            version = cursor.fetchone()[0]
        return f"连接成功，MySQL/MariaDB 版本：{version}"
    finally:
        connection.close()


def list_mysql_databases(config: dict) -> list[str]:
    connection = _mysql_connect(config)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SHOW DATABASES")
            names = [str(row[0]) for row in cursor.fetchall()]
        system = {"information_schema", "performance_schema", "mysql", "sys"}
        return sorted(names, key=lambda item: (item.lower() in system, item.lower()))
    finally:
        connection.close()


def test_sqlite(path: str) -> str:
    if not path or not Path(path).is_file():
        raise BackupError("请选择有效的 SQLite 数据库文件。")
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            tables = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()[0]
        finally:
            connection.close()
    except Exception as exc:
        raise BackupError(f"SQLite 数据库检查失败：{exc}") from exc
    if integrity.lower() != "ok":
        raise BackupError(f"SQLite 完整性检查未通过：{integrity}")
    return f"连接成功，共 {tables} 个表，完整性检查通过。"


def _mysql_table_metadata(cursor, database: str) -> list[tuple[str, str]]:
    cursor.execute(
        """
        SELECT TABLE_NAME, TABLE_TYPE
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = %s
        ORDER BY TABLE_NAME
        """,
        (database,),
    )
    return [(str(name), str(kind)) for name, kind in cursor.fetchall()]


def _mysql_insert_columns(cursor, database: str, table: str) -> list[str]:
    cursor.execute(
        """
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s
          AND TABLE_NAME = %s
          AND EXTRA NOT LIKE '%%GENERATED%%'
        ORDER BY ORDINAL_POSITION
        """,
        (database, table),
    )
    return [str(row[0]) for row in cursor.fetchall()]


def backup_mysql(
    config: dict,
    output_dir: Path,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    notify = progress or (lambda _message: None)
    database = str(config.get("mysql_database", "")).strip()
    if not database:
        raise BackupError("请先选择要备份的 MySQL 数据库。")

    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_filename_part(database)}_{_timestamp()}"
    final_path = output_dir / f"{stem}.sql"
    temp_path = output_dir / f".{stem}.sql.part"

    notify("正在连接数据库…")
    connection = _mysql_connect(config, database)
    completed = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME
                FROM information_schema.SCHEMATA
                WHERE SCHEMA_NAME = %s
                """,
                (database,),
            )
            charset_row = cursor.fetchone()
            charset = charset_row[0] if charset_row else "utf8mb4"
            collation = charset_row[1] if charset_row else "utf8mb4_unicode_ci"
            cursor.execute("SELECT VERSION()")
            server_version = str(cursor.fetchone()[0])
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            metadata = _mysql_table_metadata(cursor, database)

        tables = [name for name, kind in metadata if kind.upper() == "BASE TABLE"]
        views = [name for name, kind in metadata if kind.upper() == "VIEW"]

        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            _write_text(
                handle,
                "-- Data Backup Assistant 1.1.0\n"
                f"-- Generated: {dt.datetime.now().isoformat(timespec='seconds')}\n"
                f"-- Database: {database}\n"
                f"-- Server: MySQL/MariaDB {server_version}\n"
                "-- Restore example: mysql -u root -p < backup.sql\n\n"
                "SET NAMES utf8mb4;\n"
                "SET FOREIGN_KEY_CHECKS=0;\n"
                "SET UNIQUE_CHECKS=0;\n"
                "SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO';\n\n"
                f"CREATE DATABASE IF NOT EXISTS {_quote_identifier(database)} "
                f"DEFAULT CHARACTER SET {charset} COLLATE {collation};\n"
                f"USE {_quote_identifier(database)};\n\n",
            )

            with connection.cursor() as cursor:
                for index, table in enumerate(tables, start=1):
                    notify(f"正在导出表结构 {index}/{len(tables)}：{table}")
                    cursor.execute(f"SHOW CREATE TABLE {_quote_identifier(table)}")
                    create_sql = str(cursor.fetchone()[1])
                    _write_text(handle, f"DROP TABLE IF EXISTS {_quote_identifier(table)};")
                    _write_text(handle, create_sql + ";\n")
                if tables:
                    handle.write("\n")

            for table in views:
                notify(f"正在导出视图：{table}")
                with connection.cursor() as cursor:
                    cursor.execute(f"SHOW CREATE VIEW {_quote_identifier(table)}")
                    create_view = str(cursor.fetchone()[1])
                _write_text(handle, f"DROP VIEW IF EXISTS {_quote_identifier(table)};")
                _write_text(handle, create_view + ";\n")

            if views:
                handle.write("\n")

            for table_index, table in enumerate(tables, start=1):
                notify(f"正在导出数据 {table_index}/{len(tables)}：{table}")
                with connection.cursor() as cursor:
                    columns = _mysql_insert_columns(cursor, database, table)
                if not columns:
                    continue
                column_sql = ", ".join(_quote_identifier(column) for column in columns)
                select_sql = ", ".join(_quote_identifier(column) for column in columns)
                insert_prefix = (
                    f"INSERT INTO {_quote_identifier(table)} ({column_sql}) VALUES\n"
                )

                try:
                    import pymysql

                    stream_cursor = connection.cursor(pymysql.cursors.SSCursor)
                except Exception as exc:
                    raise BackupError(f"无法创建 MySQL 数据游标：{exc}") from exc
                try:
                    stream_cursor.execute(
                        f"SELECT {select_sql} FROM {_quote_identifier(table)}"
                    )
                    row_count = 0
                    while True:
                        rows = stream_cursor.fetchmany(200)
                        if not rows:
                            break
                        values = []
                        for row in rows:
                            values.append(
                                "(" + ",".join(_sql_literal(value) for value in row) + ")"
                            )
                        handle.write(insert_prefix)
                        handle.write(",\n".join(values))
                        handle.write(";\n")
                        row_count += len(rows)
                    if row_count:
                        handle.write("\n")
                finally:
                    stream_cursor.close()

            with connection.cursor() as cursor:
                cursor.execute(
                    "SHOW PROCEDURE STATUS WHERE Db = %s",
                    (database,),
                )
                procedures = [str(row[1]) for row in cursor.fetchall()]
                cursor.execute(
                    "SHOW FUNCTION STATUS WHERE Db = %s",
                    (database,),
                )
                functions = [str(row[1]) for row in cursor.fetchall()]
                cursor.execute("SHOW TRIGGERS")
                triggers = [str(row[0]) for row in cursor.fetchall()]
                cursor.execute("SHOW EVENTS")
                events = [str(row[1]) for row in cursor.fetchall()]

            stored_objects: list[str] = []
            for name in procedures:
                notify(f"正在导出存储过程：{name}")
                with connection.cursor() as cursor:
                    cursor.execute(f"SHOW CREATE PROCEDURE {_quote_identifier(name)}")
                    stored_objects.append("DROP PROCEDURE IF EXISTS "
                                          f"{_quote_identifier(name)};;\n"
                                          f"{cursor.fetchone()[2]};;")
            for name in functions:
                notify(f"正在导出函数：{name}")
                with connection.cursor() as cursor:
                    cursor.execute(f"SHOW CREATE FUNCTION {_quote_identifier(name)}")
                    stored_objects.append("DROP FUNCTION IF EXISTS "
                                          f"{_quote_identifier(name)};;\n"
                                          f"{cursor.fetchone()[2]};;")
            for name in triggers:
                notify(f"正在导出触发器：{name}")
                with connection.cursor() as cursor:
                    cursor.execute(f"SHOW CREATE TRIGGER {_quote_identifier(name)}")
                    stored_objects.append("DROP TRIGGER IF EXISTS "
                                          f"{_quote_identifier(name)};;\n"
                                          f"{cursor.fetchone()[2]};;")
            for name in events:
                notify(f"正在导出事件：{name}")
                with connection.cursor() as cursor:
                    cursor.execute(f"SHOW CREATE EVENT {_quote_identifier(name)}")
                    stored_objects.append("DROP EVENT IF EXISTS "
                                          f"{_quote_identifier(name)};;\n"
                                          f"{cursor.fetchone()[3]};;")

            if stored_objects:
                handle.write("DELIMITER ;;\n\n")
                handle.write("\n\n".join(stored_objects))
                handle.write("\n\nDELIMITER ;\n\n")

            _write_text(handle, "SET UNIQUE_CHECKS=1;")
            _write_text(handle, "SET FOREIGN_KEY_CHECKS=1;")
            _write_text(handle, "-- Backup completed successfully.")

        connection.commit()
        completed = True
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"MySQL 备份失败：{exc}") from exc
    finally:
        try:
            connection.close()
        except Exception:
            pass
        if completed and temp_path.exists():
            try:
                os.replace(temp_path, final_path)
            except OSError as exc:
                temp_path.unlink(missing_ok=True)
                raise BackupError(f"无法完成备份文件写入：{exc}") from exc
        elif not completed:
            temp_path.unlink(missing_ok=True)

    size = final_path.stat().st_size
    notify(f"备份完成：{final_path.name}（{_format_size(size)}）")
    return BackupResult(final_path, size, database)


def backup_sqlite(
    config: dict,
    output_dir: Path,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    notify = progress or (lambda _message: None)
    source_path = Path(str(config.get("sqlite_path", ""))).expanduser()
    if not source_path.is_file():
        raise BackupError("SQLite 数据库文件不存在。")

    database = source_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{safe_filename_part(database)}_{_timestamp()}"
    final_path = output_dir / f"{stem}.sqlite3"
    temp_path = output_dir / f".{stem}.sqlite3.part"

    notify("正在创建 SQLite 一致性快照…")
    try:
        source_uri = source_path.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=30)
        try:
            destination = sqlite3.connect(temp_path)
            try:
                source.backup(destination, pages=200, sleep=0.01)
                result = destination.execute("PRAGMA integrity_check").fetchone()[0]
                if str(result).lower() != "ok":
                    raise BackupError(f"备份文件完整性检查失败：{result}")
            finally:
                destination.close()
        finally:
            source.close()
        os.replace(temp_path, final_path)
    except BackupError:
        temp_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        raise BackupError(f"SQLite 备份失败：{exc}") from exc

    size = final_path.stat().st_size
    notify(f"备份完成：{final_path.name}（{_format_size(size)}）")
    return BackupResult(final_path, size, database)


def apply_retention(output_dir: Path, database: str, retention_days: int) -> int:
    if retention_days <= 0 or not output_dir.is_dir():
        return 0
    cutoff = dt.datetime.now().timestamp() - retention_days * 86400
    prefix = safe_filename_part(database) + "_"
    backup_pattern = re.compile(
        rf"^{re.escape(prefix)}\d{{8}}_\d{{6}}\.(?:sql|sqlite3)$",
        re.IGNORECASE,
    )
    removed = 0
    for path in output_dir.iterdir():
        if not path.is_file() or not backup_pattern.fullmatch(path.name):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def run_backup(
    config: dict,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    output_value = str(config.get("output_dir", "")).strip()
    if not output_value:
        raise BackupError("请选择备份文件保存目录。")
    output_dir = Path(output_value).expanduser()
    db_type = str(config.get("db_type", "mysql")).lower()
    if db_type == "sqlite":
        result = backup_sqlite(config, output_dir, progress)
    elif db_type == "mysql":
        result = backup_mysql(config, output_dir, progress)
    else:
        raise BackupError(f"不支持的数据库类型：{db_type}")
    try:
        days = int(config.get("retention_days", 30))
    except (TypeError, ValueError):
        days = 30
    apply_retention(output_dir, result.database, days)
    return result

