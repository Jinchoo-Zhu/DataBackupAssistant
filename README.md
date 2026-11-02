# 数据自动备份助手

English name: **DataBackupAssistant**

作者：Jinchoo（朱锦春）

版本：V1.0.0

DataBackupAssistant 是一个面向 Windows 的本地自动备份工具。每个数据库、SQLite 文件、文件或文件夹都是独立任务，可分别设置 ZIP 保存目录、执行时间和保留天数。

![界面预览](docs/app-preview.png)

## 功能

- 独立备份任务：每个数据库或文件来源分别拥有输出目录、执行时间和保留策略。
- 多数据库：可以同时添加多台 MySQL / MariaDB 服务器，支持不同端口和不同数据库版本。
- MySQL / MariaDB：导出 SQL 后压缩为 ZIP。
- SQLite：生成一致性快照后压缩为 ZIP。
- 文件与文件夹：直接生成 ZIP，保留目录结构和空文件夹。
- 添加多个数据库：一次选择多个数据库时，会拆分为多个可独立编辑的任务。
- 独立定时：程序每分钟检查任务计划，启动较晚时可补做当天未执行任务。
- 新增任务不会立即补做：如果添加任务时当天的执行时间已经过去，会等待下一次计划时间。
- 自动清理：每个任务按自己的名称和保留天数清理历史 ZIP。
- 后台运行：关闭窗口后隐藏到系统托盘，右键托盘可以打开主界面、立即备份或退出。
- 开机自启：通过当前用户注册表启动，不需要管理员权限。
- 可靠性：先写临时文件，备份完成并通过校验后再生成正式文件。
- 密码保护：MySQL 密码使用 Windows DPAPI 按当前用户加密保存。
- 文件容错：可选择跳过无法读取的文件，并在 ZIP 内的备份说明中记录跳过项。

## MySQL 备份内容

MySQL / MariaDB 备份生成 ZIP，内部包含 `.sql` 文件。SQL 内容包括：

- 建库语句与表结构。
- 表数据、生成列处理。
- 视图、存储过程、函数、触发器和事件。
- 外键关闭与恢复指令，便于跨表恢复。

InnoDB 数据使用一致性事务快照导出。MyISAM 表无法提供同等的事务级一致性保证。

推荐为备份创建专用账号，至少授予：

```sql
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT
ON `你的数据库`.* TO 'backup_user'@'%';

GRANT SHOW_ROUTINE ON *.* TO 'backup_user'@'%';
```

## 文件备份说明

- 文件任务以来源文件或文件夹名称作为 ZIP 内的根目录。
- ZIP 中会包含 `备份说明.txt`，记录任务名称、备份时间和原文件路径；如有跳过项会追加记录。
- 如果保存目录位于某个来源文件夹内部，程序会自动排除保存目录，避免备份文件递归打包自身。
- 空文件夹也会被保留。
- 文件备份不是磁盘快照。正在被独占写入的文件可能无法读取；建议启用“跳过无法读取的文件”，或先停止相关应用。

## SQLite 备份

SQLite 使用官方在线备份接口生成一致性 `.sqlite3` 快照，随后压缩为 ZIP，并执行完整性检查。

## 快速开始

```powershell
git clone git@github.com:Jinchoo-zhu/DataBackupAssistant.git
cd DataBackupAssistant

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python src\app.py
```

## 使用流程

1. 点击“添加 MySQL”“添加 SQLite”或“添加文件/文件夹”。
2. 填写任务名称并选择来源。MySQL 可选择多个数据库，保存时自动拆成独立任务。
3. 文件任务先选择“文件”或“文件夹”类型，再点击“浏览”打开对应的选择窗口。
4. 在任务中添加该任务自己的保存目录、每天执行时间和保留天数。
5. 双击列表中的任务可以修改，勾选后点击“删除勾选任务”可批量删除。
6. 勾选任务后点击“备份勾选任务”可立即验证。
7. 点击“保存设置”保存全部任务。

## 备份文件命名

```text
【YYYYMMDDHHMMSS】 任务名称 - 来源名称.zip
```

同一目录、同一秒产生多个任务时，后续文件自动命名为
`【YYYYMMDDHHMMSS】 任务名称 - 来源名称_2.zip`、`_3.zip`，不会覆盖。

## 恢复

MySQL：

```powershell
Expand-Archive '.\【20261101020000】 任务名称 - 来源名称.zip' -DestinationPath .\restore
mysql -u root -p < .\restore\数据库名.sql
```

SQLite：

1. 解压 ZIP。
2. 停止正在写入数据库的程序。
3. 保留原数据库文件副本。
4. 用解压出的原始 SQLite 文件替换原数据库文件。

文件：

使用任意 ZIP 工具解压。备份说明文件仅用于记录本次任务，不需要恢复到业务目录。

## 文件位置

- 配置：`%APPDATA%\DataBackupAssistant\config.json`
- 日志：`%APPDATA%\DataBackupAssistant\logs\backup.log`
- 默认备份目录：`%USERPROFILE%\Documents\DataBackups`

旧名称 `DatabaseBackupAssistant` 的配置会在首次启动时自动迁移。

## 命令行

```powershell
# 自检数据库、文件 ZIP、完整性校验、密码加密和保留策略
python src\app.py --self-test

# 使用当前配置执行一次备份
python src\app.py --backup-once

# 隐藏主窗口启动到系统托盘
python src\app.py --minimized
```

## 构建 EXE

使用项目虚拟环境直接运行 Spec：

```powershell
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean DataBackupAssistant.spec
```

默认生成：

```text
dist\数据自动备份助手.exe
```

也可以使用构建脚本，将成品统一输出到 `outputs`：

```powershell
python -m pip install -r requirements.txt
powershell -ExecutionPolicy Bypass -File .\build.ps1
```

生成文件：

```text
outputs\数据自动备份助手.exe
```

## 项目结构

```text
DataBackupAssistant/
├── assets/
│   ├── app.ico
│   ├── app.png
│   ├── app-48.png
│   ├── app-source.png
│   ├── checkbox-checked.png
│   └── checkbox-unchecked.png
├── docs/
│   └── app-preview.png
├── src/
│   ├── app.py
│   ├── backup_engine.py
│   ├── config_store.py
│   ├── backup_job_dialog.py
│   └── windows_integration.py
├── DataBackupAssistant.spec
├── build.ps1
├── create_icon.py
├── requirements.txt
├── version_info.txt
├── README.md
└── 使用说明.md
```

## 后续方向

程序的数据模型和备份引擎已按“文件与数据库统一备份”拆分。后续可继续增加：

- 文件增量备份、排除规则和单文件历史版本。
- Windows VSS 卷影复制，备份正在使用的文件。
- PostgreSQL、SQL Server 等数据库引擎。
- 备份完成后同步到 NAS、对象存储或云盘。

## 注意事项

- 每个 MySQL 数据库会显示为独立任务，可分别设置目录、时间和保留策略。
- 新增或刚修改的任务如果已经错过当天时间，不会立刻自动备份；如需立即执行，请手动勾选后点击“备份勾选任务”。
- 请在备份时间保持电脑开机并登录 Windows，或让程序常驻系统托盘。
- 本地备份不能代替异地备份，重要数据应至少保留一份到其他磁盘、NAS 或云端。
- 首次使用前请执行一次“立即备份”，并实际验证恢复流程。
