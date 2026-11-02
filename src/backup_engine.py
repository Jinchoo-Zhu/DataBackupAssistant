from __future__ import annotations

import datetime as dt
import math
import os
import re
import sqlite3
import zipfile
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
    source_name: str


def safe_filename_part(value: str, fallback: str = "backup") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value.strip())
    cleaned = cleaned.strip(" .")
    return cleaned[:80] or fallback


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _archive_timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d%H%M%S")


def _archive_stem(
    job_name: str,
    source_name: str,
    timestamp: str | None = None,
) -> str:
    job_name = job_name.strip() or "备份任务"
    source_name = source_name.strip()
    label = (
        job_name
        if source_name and job_name.endswith(f" - {source_name}")
        else f"{job_name} - {source_name}"
        if source_name
        else job_name
    )
    return (
        f"【{timestamp or _archive_timestamp()}】 "
        f"{safe_filename_part(label, fallback='备份')}"
    )


def _unique_archive_path(output_dir: Path, stem: str) -> Path:
    candidate = output_dir / f"{stem}.zip"
    suffix = 2
    while candidate.exists():
        candidate = output_dir / f"{stem}_{suffix}.zip"
        suffix += 1
    return candidate


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


def _file_sources(config: dict) -> list[Path]:
    raw = config.get("file_sources", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise BackupError("文件备份来源配置无效。")
    sources: list[Path] = []
    seen: set[str] = set()
    for value in raw:
        text = str(value).strip()
        if not text:
            continue
        path = Path(text).expanduser()
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            sources.append(path)
            seen.add(key)
    return sources


def test_file_sources(sources: list[str]) -> str:
    paths = _file_sources({"file_sources": sources})
    if not paths:
        raise BackupError("请至少添加一个文件或文件夹。")
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        raise BackupError("以下来源不存在：\n" + "\n".join(missing))
    return f"路径检查通过，共 {len(paths)} 个备份来源。"


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_reparse_point(path: Path) -> bool:
    try:
        return path.is_symlink() or os.path.isjunction(path)
    except OSError:
        return True


def _unique_archive_roots(sources: list[Path]) -> list[tuple[Path, str]]:
    used: set[str] = set()
    result: list[tuple[Path, str]] = []
    for source in sources:
        base = source.name or source.drive.replace(":", "") or "root"
        base = safe_filename_part(base, fallback="source")
        candidate = base
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        result.append((source, candidate))
    return result


def backup_files(
    config: dict,
    output_dir: Path,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    notify = progress or (lambda _message: None)
    sources = _file_sources(config)
    if not sources:
        raise BackupError("请至少添加一个文件或文件夹。")
    missing = [str(path) for path in sources if not path.exists()]
    if missing:
        raise BackupError("以下备份来源不存在：\n" + "\n".join(missing))

    resolved_output = output_dir.resolve()
    for source in sources:
        if source.is_dir() and _is_within(resolved_output, source) and (
            resolved_output == source.resolve()
        ):
            raise BackupError(
                f"备份保存目录不能与来源文件夹相同：{source}"
            )

    backup_name = str(config.get("file_backup_name", "")).strip() or "文件备份"
    skip_unreadable = bool(config.get("file_skip_unreadable", True))
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = dt.datetime.now()
    source_label = (
        sources[0].name
        or sources[0].drive.replace(":", "")
        or f"{len(sources)}个来源"
    )
    stem = _archive_stem(
        backup_name,
        source_label,
        started_at.strftime("%Y%m%d%H%M%S"),
    )
    final_path = _unique_archive_path(output_dir, stem)
    temp_path = output_dir / f".{stem}.zip.part"

    sources_with_roots = _unique_archive_roots(sources)
    skipped: list[str] = []
    file_count = 0
    completed = False

    notify(f"正在创建文件备份：{backup_name}")
    try:
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.comment = (
                f"DBA_JOB_ID={config.get('backup_job_id', '')}".encode("utf-8")
            )
            for source, root_name in sources_with_roots:
                if source.is_file():
                    file_count += 1
                    try:
                        archive.write(source, root_name)
                    except OSError as exc:
                        if not skip_unreadable:
                            raise BackupError(f"无法读取文件：{source}（{exc}）") from exc
                        skipped.append(str(source))
                    continue

                if not source.is_dir():
                    if not skip_unreadable:
                        raise BackupError(f"不支持的来源类型：{source}")
                    skipped.append(str(source))
                    continue

                archive.writestr(root_name + "/", b"")
                walk_error: list[OSError] = []

                def on_walk_error(exc: OSError) -> None:
                    if skip_unreadable:
                        skipped.append(str(exc))
                    else:
                        walk_error.append(exc)

                for current, dirnames, filenames in os.walk(
                    source,
                    topdown=True,
                    onerror=on_walk_error,
                    followlinks=False,
                ):
                    if walk_error:
                        raise BackupError(
                            f"无法读取文件夹：{current}（{walk_error[0]}）"
                        ) from walk_error[0]

                    current_path = Path(current)
                    allowed_dirs = []
                    for dirname in sorted(dirnames):
                        directory = current_path / dirname
                        if _is_reparse_point(directory):
                            skipped.append(str(directory))
                            continue
                        if _is_within(directory, resolved_output):
                            continue
                        allowed_dirs.append(dirname)
                        relative = directory.relative_to(source)
                        archive_name = (
                            Path(root_name) / relative
                        ).as_posix().rstrip("/") + "/"
                        archive.writestr(archive_name, b"")
                    dirnames[:] = allowed_dirs

                    for filename in sorted(filenames):
                        file_path = current_path / filename
                        if _is_reparse_point(file_path):
                            skipped.append(str(file_path))
                            continue
                        if _is_within(file_path, resolved_output):
                            continue
                        relative = file_path.relative_to(source)
                        archive_name = (Path(root_name) / relative).as_posix()
                        file_count += 1
                        try:
                            archive.write(file_path, archive_name)
                        except OSError as exc:
                            if not skip_unreadable:
                                raise BackupError(
                                    f"无法读取文件：{file_path}（{exc}）"
                                ) from exc
                            skipped.append(str(file_path))
                            file_count -= 1
                        if file_count == 1 or file_count % 100 == 0:
                            notify(f"正在打包文件：已处理 {file_count} 个")

            report_lines = [
                f"任务: {backup_name}",
                f"备份时间: {started_at:%Y-%m-%d %H:%M:%S}",
                "原文件路径: " + "；".join(str(source) for source in sources),
            ]
            if skipped:
                report_lines.extend(["", "跳过项:", *skipped])
            archive.writestr(
                "备份说明.txt",
                "\r\n".join(report_lines) + "\r\n",
            )

        os.replace(temp_path, final_path)
        with zipfile.ZipFile(final_path, mode="r", allowZip64=True) as archive:
            bad_entry = archive.testzip()
            if bad_entry is not None:
                final_path.unlink(missing_ok=True)
                raise BackupError(f"ZIP 完整性检查失败：{bad_entry}")
        completed = True
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError(f"文件备份失败：{exc}") from exc
    finally:
        if not completed:
            temp_path.unlink(missing_ok=True)

    size = final_path.stat().st_size
    skipped_text = f"，跳过 {len(skipped)} 项" if skipped else ""
    notify(
        f"文件备份完成：{final_path.name}"
        f"（{file_count} 个文件，{_format_size(size)}{skipped_text}）"
    )
    return BackupResult(final_path, size, backup_name)


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
    target_name = str(config.get("backup_target_name", "")).strip()
    source_name = f"{target_name}_{database}" if target_name else database

    output_dir.mkdir(parents=True, exist_ok=True)
    configured_stem = str(config.get("database_archive_name", "")).strip()
    stem = (
        safe_filename_part(configured_stem, fallback=database)
        if configured_stem
        else f"{safe_filename_part(source_name, fallback=database)}_{_timestamp()}"
    )
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
                "-- Data Backup Assistant 1.0.0\n"
                f"-- Generated: {dt.datetime.now().isoformat(timespec='seconds')}\n"
                f"-- Database: {database}\n"
                f"-- Connection: {target_name or 'default'}\n"
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
    return BackupResult(final_path, size, source_name)


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
    target_name = str(config.get("backup_target_name", "")).strip()
    source_name = f"{target_name}_{database}" if target_name else database
    output_dir.mkdir(parents=True, exist_ok=True)
    configured_stem = str(config.get("database_archive_name", "")).strip()
    stem = (
        safe_filename_part(configured_stem, fallback=database)
        if configured_stem
        else f"{safe_filename_part(source_name, fallback=database)}_{_timestamp()}"
    )
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
    return BackupResult(final_path, size, source_name)


def apply_retention(
    output_dir: Path,
    source_name: str,
    retention_days: int,
    job_id: str = "",
) -> int:
    if retention_days <= 0 or not output_dir.is_dir():
        return 0
    cutoff = dt.datetime.now().timestamp() - retention_days * 86400
    prefix = safe_filename_part(source_name) + "_"
    backup_pattern = re.compile(
        rf"^{re.escape(prefix)}\d{{8}}_\d{{6}}\.(?:sql|sqlite3|zip)$",
        re.IGNORECASE,
    )
    timestamp_pattern = re.compile(
        r"^【\d{14}】 .+(?:_\d+)?\.zip$",
        re.IGNORECASE,
    )
    removed = 0
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        is_legacy = bool(backup_pattern.fullmatch(path.name))
        is_timestamped = bool(timestamp_pattern.fullmatch(path.name))
        if not is_legacy and not is_timestamped:
            continue
        if is_timestamped and job_id:
            try:
                with zipfile.ZipFile(path, mode="r") as archive:
                    owner = archive.comment.decode("utf-8", errors="replace")
                if owner != f"DBA_JOB_ID={job_id}":
                    continue
            except (OSError, zipfile.BadZipFile):
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


def _zip_database_result(
    raw_result: BackupResult,
    output_dir: Path,
    job_name: str,
    source_name: str,
    job_type: str,
    detail_label: str,
    detail_value: str,
    archive_entry_name: str | None = None,
    job_id: str = "",
    progress: ProgressCallback | None = None,
) -> BackupResult:
    notify = progress or (lambda _message: None)
    started_at = dt.datetime.now()
    stem = _archive_stem(
        job_name,
        source_name,
        started_at.strftime("%Y%m%d%H%M%S"),
    )
    final_path = _unique_archive_path(output_dir, stem)
    temp_path = output_dir / f".{stem}.zip.part"
    completed = False
    try:
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.comment = f"DBA_JOB_ID={job_id}".encode("utf-8")
            archive.write(
                raw_result.path,
                arcname=archive_entry_name or raw_result.path.name,
                compress_type=zipfile.ZIP_DEFLATED,
            )
            report = (
                f"任务: {job_name}\r\n"
                f"备份时间: {started_at:%Y-%m-%d %H:%M:%S}\r\n"
                f"{detail_label}: {detail_value}\r\n"
            )
            archive.writestr("备份说明.txt", report)
        os.replace(temp_path, final_path)
        with zipfile.ZipFile(final_path, mode="r", allowZip64=True) as archive:
            bad_entry = archive.testzip()
            if bad_entry is not None:
                final_path.unlink(missing_ok=True)
                raise BackupError(f"ZIP 完整性检查失败：{bad_entry}")
        completed = True
    finally:
        if not completed:
            temp_path.unlink(missing_ok=True)
    raw_result.path.unlink(missing_ok=True)
    size = final_path.stat().st_size
    notify(f"数据库备份已压缩：{final_path.name}（{_format_size(size)}）")
    return BackupResult(final_path, size, job_name)


def run_backup_job(
    job: dict,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    output_value = str(job.get("output_dir", "")).strip()
    if not output_value:
        raise BackupError(f"{job.get('name', '备份任务')} 未设置保存目录。")
    output_dir = Path(output_value).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    job_name = str(job.get("name", "")).strip() or "备份任务"
    job_type = str(job.get("job_type", "")).lower()

    if job_type == "mysql":
        database = str(job.get("database", "")).strip()
        if not database:
            raise BackupError(f"{job_name} 未选择 MySQL 数据库。")
        mysql_host = str(job.get("host", "127.0.0.1"))
        mysql_port = int(job.get("port", 3306))
        archive_content_name = database
        raw_result = backup_mysql(
            {
                "mysql_host": mysql_host,
                "mysql_port": mysql_port,
                "mysql_user": job.get("user", "root"),
                "mysql_password": job.get("password", ""),
                "mysql_database": database,
                "backup_target_name": job_name,
                "database_archive_name": archive_content_name,
            },
            output_dir,
            progress,
        )
        sql_file_name = raw_result.path.name
        result = _zip_database_result(
            raw_result,
            output_dir,
            job_name,
            database,
            "MySQL",
            "备份内容",
            sql_file_name,
            sql_file_name,
            str(job.get("id", "")),
            progress,
        )
    elif job_type == "sqlite":
        sqlite_path = str(job.get("sqlite_path", ""))
        source_path = Path(sqlite_path)
        raw_result = backup_sqlite(
            {
                "sqlite_path": sqlite_path,
                "backup_target_name": job_name,
                "database_archive_name": source_path.stem,
            },
            output_dir,
            progress,
        )
        result = _zip_database_result(
            raw_result,
            output_dir,
            job_name,
            source_path.name,
            "SQLite",
            "原文件路径",
            sqlite_path,
            source_path.name,
            str(job.get("id", "")),
            progress,
        )
    elif job_type == "files":
        result = backup_files(
            {
                "file_backup_name": job_name,
                "file_sources": [job.get("file_source", "")],
                "file_skip_unreadable": bool(
                    job.get("file_skip_unreadable", True)
                ),
                "backup_job_id": str(job.get("id", "")),
            },
            output_dir,
            progress,
        )
    else:
        raise BackupError(f"不支持的备份任务类型：{job_type}")

    try:
        retention_days = int(job.get("retention_days", 30))
    except (TypeError, ValueError):
        retention_days = 30
    apply_retention(
        output_dir,
        job_name,
        retention_days,
        job_id=str(job.get("id", "")),
    )
    return result


def _database_targets(config: dict) -> list[dict]:
    if "database_targets" in config:
        targets = config.get("database_targets")
        if not isinstance(targets, list):
            raise BackupError("数据库连接配置格式无效。")
        return [dict(target) for target in targets if isinstance(target, dict)]

    db_type = str(config.get("db_type", "mysql")).lower()
    if db_type == "mysql" and str(config.get("mysql_database", "")).strip():
        return [
            {
                "name": "",
                "db_type": "mysql",
                "host": config.get("mysql_host", "127.0.0.1"),
                "port": config.get("mysql_port", 3306),
                "user": config.get("mysql_user", "root"),
                "password": config.get("mysql_password", ""),
                "databases": [config.get("mysql_database")],
            }
        ]
    if db_type == "sqlite" and str(config.get("sqlite_path", "")).strip():
        return [
            {
                "name": "",
                "db_type": "sqlite",
                "sqlite_paths": [config.get("sqlite_path")],
            }
        ]
    return []


def run_backup_tasks(
    config: dict,
    progress: ProgressCallback | None = None,
) -> list[BackupResult]:
    output_value = str(config.get("output_dir", "")).strip()
    if not output_value:
        raise BackupError("请选择备份文件保存目录。")
    output_dir = Path(output_value).expanduser()

    database_enabled = bool(config.get("database_backup_enabled", True))
    file_enabled = bool(config.get("file_backup_enabled", False))
    if not database_enabled and not file_enabled:
        raise BackupError("请至少启用数据库备份或文件备份中的一种。")

    results: list[BackupResult] = []
    errors: list[str] = []
    if database_enabled:
        targets = _database_targets(config)
        if not targets:
            raise BackupError("请至少添加一个数据库备份配置。")
        for target_index, target in enumerate(targets, start=1):
            target_name = str(target.get("name", "")).strip()
            db_type = str(target.get("db_type", "mysql")).lower()
            label = target_name or f"数据库配置 {target_index}"
            try:
                if db_type == "mysql":
                    databases = target.get("databases", [])
                    if isinstance(databases, str):
                        databases = [databases]
                    databases = [str(name).strip() for name in databases if str(name).strip()]
                    if not databases:
                        raise BackupError(f"{label} 尚未选择数据库。")
                    for database in databases:
                        task_config = dict(config)
                        task_config.update(
                            {
                                "mysql_host": target.get("host", "127.0.0.1"),
                                "mysql_port": target.get("port", 3306),
                                "mysql_user": target.get("user", "root"),
                                "mysql_password": target.get("password", ""),
                                "mysql_database": database,
                                "backup_target_name": target_name,
                            }
                        )
                        if progress:
                            progress(f"正在备份数据库：{label} / {database}")
                        results.append(
                            backup_mysql(task_config, output_dir, progress)
                        )
                elif db_type == "sqlite":
                    paths = target.get("sqlite_paths", [])
                    if isinstance(paths, str):
                        paths = [paths]
                    paths = [str(path).strip() for path in paths if str(path).strip()]
                    if not paths:
                        raise BackupError(f"{label} 尚未选择 SQLite 文件。")
                    for sqlite_path in paths:
                        task_config = dict(config)
                        task_config.update(
                            {
                                "sqlite_path": sqlite_path,
                                "backup_target_name": target_name,
                            }
                        )
                        if progress:
                            progress(
                                f"正在备份 SQLite：{label} / {Path(sqlite_path).name}"
                            )
                        results.append(
                            backup_sqlite(task_config, output_dir, progress)
                        )
                else:
                    raise BackupError(f"不支持的数据库类型：{db_type}")
            except Exception as exc:
                message = f"{label}：{exc}"
                errors.append(message)
                if progress:
                    progress(f"备份失败：{message}")
    if file_enabled:
        try:
            results.append(backup_files(config, output_dir, progress))
        except Exception as exc:
            errors.append(f"文件备份：{exc}")
            if progress:
                progress(f"备份失败：文件备份：{exc}")

    try:
        days = int(config.get("retention_days", 30))
    except (TypeError, ValueError):
        days = 30
    for result in results:
        apply_retention(output_dir, result.source_name, days)
    if errors:
        raise BackupError("部分备份任务失败：\n" + "\n".join(errors))
    return results


def run_backup(
    config: dict,
    progress: ProgressCallback | None = None,
) -> BackupResult:
    results = run_backup_tasks(config, progress)
    if len(results) != 1:
        raise BackupError("当前配置包含多个备份任务，请使用 run_backup_tasks。")
    return results[0]

