from __future__ import annotations

import base64
import hashlib
import hmac
import json
import smtplib
import ssl
import subprocess
import time
from copy import deepcopy
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

DEFAULT_NOTIFICATION_SETTINGS: dict[str, Any] = {
    "notify_on_success": True,
    "notify_on_failure": True,
    "backup": {
        "email_enabled": False,
        "dingtalk_enabled": False,
    },
    "email": {
        "smtp_host": "",
        "smtp_port": 465,
        "security": "ssl",
        "username": "",
        "password_enc": "",
        "sender": "",
        "recipients": "",
    },
    "dingtalk": {
        "webhook_enc": "",
        "secret_enc": "",
        "at_mobiles": "",
    },
    "windows_backup": {
        "enabled": False,
        "provider": "scheduled_task",
        "email_enabled": False,
        "dingtalk_enabled": False,
        "task_path": "",
        "task_name": "",
        "baseline_initialized": False,
        "last_observed_run_time": "",
        "last_observed_result": "",
        "last_observed_state": "",
    },
}


class NotificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryResult:
    channel: str
    success: bool
    message: str


WINDOWS_SERVER_BACKUP_PROVIDER = "windows_server_backup"


def notification_settings(value: object) -> dict[str, Any]:
    settings = deepcopy(DEFAULT_NOTIFICATION_SETTINGS)
    if not isinstance(value, dict):
        return settings
    for key in ("notify_on_success", "notify_on_failure"):
        if key in value:
            settings[key] = bool(value[key])
    for section in ("backup", "email", "dingtalk", "windows_backup"):
        raw_section = value.get(section)
        if not isinstance(raw_section, dict):
            continue
        for key, default in settings[section].items():
            if key not in raw_section:
                continue
            if isinstance(default, bool):
                settings[section][key] = bool(raw_section[key])
            elif isinstance(default, int):
                try:
                    settings[section][key] = int(raw_section[key])
                except (TypeError, ValueError):
                    pass
            else:
                settings[section][key] = str(raw_section[key])
    return settings


def _recipients(value: object) -> list[str]:
    text = str(value or "").replace(";", ",").replace("\n", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def _mobiles(value: object) -> list[str]:
    return _recipients(value)


def send_email(
    settings: dict[str, Any],
    subject: str,
    body: str,
) -> None:
    config = settings.get("email", {})
    if not isinstance(config, dict):
        raise NotificationError("邮件通知配置无效。")
    host = str(config.get("smtp_host", "")).strip()
    recipients = _recipients(config.get("recipients", ""))
    username = str(config.get("username", "")).strip()
    sender = str(config.get("sender", "")).strip() or username
    try:
        port = int(config.get("smtp_port", 465))
    except (TypeError, ValueError) as exc:
        raise NotificationError("SMTP 端口无效。") from exc
    if not host:
        raise NotificationError("未填写 SMTP 服务器。")
    if not sender:
        raise NotificationError("未填写发件邮箱。")
    if not recipients:
        raise NotificationError("未填写收件人。")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    security = str(config.get("security", "ssl")).lower()
    password = str(config.get("password", ""))
    context = ssl.create_default_context()
    try:
        if security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                host,
                port,
                timeout=20,
                context=context,
            )
        else:
            client = smtplib.SMTP(host, port, timeout=20)
        with client:
            client.ehlo()
            if security == "starttls":
                client.starttls(context=context)
                client.ehlo()
            if username:
                client.login(username, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise NotificationError(f"邮件发送失败：{exc}") from exc


def _signed_dingtalk_url(webhook: str, secret: str) -> str:
    parts = urlsplit(webhook)
    if parts.scheme != "https" or not parts.netloc:
        raise NotificationError("钉钉 Webhook 必须是有效的 HTTPS 地址。")
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if secret:
        timestamp = str(round(time.time() * 1000))
        signature = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}\n{secret}".encode(),
            digestmod=hashlib.sha256,
        ).digest()
        query["timestamp"] = timestamp
        query["sign"] = base64.b64encode(signature).decode("ascii")
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def send_dingtalk(
    settings: dict[str, Any],
    subject: str,
    body: str,
) -> None:
    config = settings.get("dingtalk", {})
    if not isinstance(config, dict):
        raise NotificationError("钉钉通知配置无效。")
    webhook = str(config.get("webhook", "")).strip()
    if not webhook:
        raise NotificationError("未填写钉钉机器人 Webhook。")
    payload: dict[str, Any] = {
        "msgtype": "markdown",
        "markdown": {
            "title": subject,
            "text": f"### {subject}\n\n{body}",
        },
    }
    mobiles = _mobiles(config.get("at_mobiles", ""))
    if mobiles:
        payload["at"] = {"atMobiles": mobiles, "isAtAll": False}
        payload["markdown"]["text"] += "\n\n" + " ".join(
            f"@{mobile}" for mobile in mobiles
        )
    request = Request(
        _signed_dingtalk_url(webhook, str(config.get("secret", ""))),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
        result = json.loads(raw)
        if int(result.get("errcode", -1)) != 0:
            raise NotificationError(
                f"钉钉返回错误：{result.get('errmsg', raw)}"
            )
    except NotificationError:
        raise
    except (OSError, ValueError) as exc:
        raise NotificationError(f"钉钉发送失败：{exc}") from exc


def send_notifications(
    settings: dict[str, Any],
    subject: str,
    body: str,
    channels: set[str] | None = None,
) -> list[DeliveryResult]:
    results: list[DeliveryResult] = []
    for name, key, channel, sender in (
        ("邮件", "email", settings.get("email", {}), send_email),
        ("钉钉", "dingtalk", settings.get("dingtalk", {}), send_dingtalk),
    ):
        if channels is not None and key not in channels:
            continue
        if not isinstance(channel, dict):
            continue
        try:
            sender(settings, subject, body)
            results.append(DeliveryResult(name, True, "发送成功"))
        except Exception as exc:  # noqa: BLE001
            results.append(DeliveryResult(name, False, str(exc)))
    return results


def notification_channels(
    settings: dict[str, Any],
    scope: str,
) -> set[str]:
    section = settings.get(scope, {})
    if not isinstance(section, dict):
        return set()
    channels: set[str] = set()
    if section.get("email_enabled"):
        channels.add("email")
    if section.get("dingtalk_enabled"):
        channels.add("dingtalk")
    return channels


def channel_is_configured(settings: dict[str, Any], channel: str) -> bool:
    if channel == "email":
        email = settings.get("email", {})
        return bool(
            isinstance(email, dict)
            and str(email.get("smtp_host", "")).strip()
            and str(email.get("recipients", "")).strip()
            and (
                str(email.get("sender", "")).strip()
                or str(email.get("username", "")).strip()
            )
        )
    if channel == "dingtalk":
        dingtalk = settings.get("dingtalk", {})
        return bool(
            isinstance(dingtalk, dict)
            and (
                str(dingtalk.get("webhook_enc", "")).strip()
                or str(dingtalk.get("webhook", "")).strip()
            )
        )
    return False


def _run_powershell(script: str, timeout: int = 30) -> str:
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$OutputEncoding=[Console]::OutputEncoding="
            "[System.Text.UTF8Encoding]::new($false);" + script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=creation_flags,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise NotificationError(f"读取 Windows 任务失败：{detail}")
    return completed.stdout.strip()


def list_system_backup_tasks() -> list[dict[str, Any]]:
    script = r"""
$tasks = Get-ScheduledTask -ErrorAction SilentlyContinue | Where-Object {
    $_.TaskPath -match '(?i)backup|备份' -or
    $_.TaskName -match '(?i)backup|备份' -or
    (($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -match '(?i)wbadmin|backup')
}
$items = foreach ($task in $tasks) {
    $info = Get-ScheduledTaskInfo -TaskPath $task.TaskPath -TaskName $task.TaskName -ErrorAction SilentlyContinue
    [pscustomobject]@{
        task_path = [string]$task.TaskPath
        task_name = [string]$task.TaskName
        state = [string]$task.State
        last_run_time = if ($info.LastRunTime) { $info.LastRunTime.ToString('o') } else { '' }
        next_run_time = if ($info.NextRunTime) { $info.NextRunTime.ToString('o') } else { '' }
        last_task_result = [string]$info.LastTaskResult
    }
}
@($items) | ConvertTo-Json -Compress -Depth 3
"""
    output = _run_powershell(script)
    if not output:
        return []
    try:
        raw = json.loads(output)
    except ValueError as exc:
        raise NotificationError(f"Windows 任务信息解析失败：{exc}") from exc
    items = raw if isinstance(raw, list) else [raw]
    tasks = [dict(item) for item in items if isinstance(item, dict)]
    for task in tasks:
        task["provider"] = "scheduled_task"
    windows_summary = get_windows_server_backup_status()
    if windows_summary is not None:
        tasks.insert(0, windows_summary)
    return tasks


def get_windows_server_backup_status() -> dict[str, Any] | None:
    script = r"""
$summary = Get-WBSummary -ErrorAction SilentlyContinue
if ($null -eq $summary) { return }
$lastRun = $summary.LastBackupTime
$nextRun = $summary.NextBackupTime
$hasRun = $null -ne $lastRun -and $lastRun.Year -gt 1900
$lastTarget = [string]$summary.LastBackupTarget
if (-not $lastTarget) {
    $lastTarget = [string]$summary.LastSuccessfulBackupTarget
}

$configuredTarget = ''
$targetPathFallback = ''
try {
    $policy = Get-WBPolicy -ErrorAction Stop
    $policyTarget = @(
        Get-WBBackupTarget -Policy $policy -ErrorAction Stop
    ) | Select-Object -First 1
    if ($null -ne $policyTarget) {
        foreach ($candidate in @(
            $policyTarget.Label,
            $policyTarget.TargetName,
            $policyTarget.Name,
            $policyTarget.FriendlyName
        )) {
            $text = [string]$candidate
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $configuredTarget = $text
                break
            }
        }
        foreach ($candidate in @(
            $policyTarget.TargetRootPath,
            $policyTarget.VolumePath,
            $policyTarget.Path
        )) {
            $text = [string]$candidate
            if (-not [string]::IsNullOrWhiteSpace($text)) {
                $targetPathFallback = $text
                break
            }
        }
        if (-not $targetPathFallback) {
            foreach ($property in $policyTarget.PSObject.Properties) {
                if ($property.Name -match '(?i)path') {
                    $text = [string]$property.Value
                    if (-not [string]::IsNullOrWhiteSpace($text)) {
                        $targetPathFallback = $text
                        break
                    }
                }
            }
        }
    }
} catch {}

if (-not $configuredTarget) {
    try {
        $backupDisk = @(
            Get-WBDisk -ErrorAction Stop |
                Where-Object { $_.IsBackupTarget }
        ) | Select-Object -First 1
        if ($null -ne $backupDisk) {
            $configuredTarget = [string]$backupDisk.FriendlyName
        }
    } catch {}
}
if (-not $configuredTarget) {
    $configuredTarget = $targetPathFallback
}
if (-not $lastTarget) {
    $lastTarget = $configuredTarget
}

[pscustomobject]@{
    provider = 'windows_server_backup'
    task_path = ''
    task_name = 'Windows Server Backup（系统计划）'
    state = if ($hasRun) { 'Ready' } else { 'NotRun' }
    last_run_time = if ($hasRun) { $lastRun.ToString('o') } else { '' }
    next_run_time = if ($nextRun) { $nextRun.ToString('o') } else { '' }
    last_task_result = if ($hasRun) { [string]$summary.LastBackupResultHR } else { '' }
    backup_target = $lastTarget
} | ConvertTo-Json -Compress
"""
    try:
        output = _run_powershell(script)
    except NotificationError:
        return None
    if not output:
        return None
    try:
        raw = json.loads(output)
    except ValueError:
        return None
    return dict(raw) if isinstance(raw, dict) else None


def get_scheduled_task_status(
    task_path: str,
    task_name: str,
) -> dict[str, Any]:
    safe_path = task_path.replace("'", "''")
    safe_name = task_name.replace("'", "''")
    script = rf"""
$task = Get-ScheduledTask -TaskPath '{safe_path}' -TaskName '{safe_name}' -ErrorAction Stop
$info = Get-ScheduledTaskInfo -TaskPath $task.TaskPath -TaskName $task.TaskName -ErrorAction Stop
[pscustomobject]@{{
    task_path = [string]$task.TaskPath
    task_name = [string]$task.TaskName
    state = [string]$task.State
    last_run_time = if ($info.LastRunTime) {{ $info.LastRunTime.ToString('o') }} else {{ '' }}
    next_run_time = if ($info.NextRunTime) {{ $info.NextRunTime.ToString('o') }} else {{ '' }}
    last_task_result = [string]$info.LastTaskResult
}} | ConvertTo-Json -Compress
"""
    output = _run_powershell(script)
    try:
        raw = json.loads(output)
    except ValueError as exc:
        raise NotificationError(f"Windows 任务状态解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise NotificationError("Windows 任务状态格式无效。")
    raw["provider"] = "scheduled_task"
    return dict(raw)


def get_system_backup_status(binding: dict[str, Any]) -> dict[str, Any]:
    if binding.get("provider") == WINDOWS_SERVER_BACKUP_PROVIDER:
        status = get_windows_server_backup_status()
        if status is None:
            raise NotificationError(
                "无法读取 Windows Server Backup 状态，请确认已安装该功能。"
            )
        return status
    return get_scheduled_task_status(
        str(binding.get("task_path", "")),
        str(binding.get("task_name", "")),
    )


def is_new_system_backup_result(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> bool:
    last_run = str(current.get("last_run_time", "")).strip()
    previous_run = str(previous.get("last_observed_run_time", "")).strip()
    if not last_run:
        return False

    state = str(current.get("state", "")).strip().casefold()
    previous_state = str(
        previous.get("last_observed_state", "")
    ).strip().casefold()
    if state in {"running", "queued", "unknown"}:
        return False
    if not previous_run:
        return bool(previous.get("baseline_initialized"))

    last_result = str(current.get("last_task_result", "")).strip()
    previous_result = str(
        previous.get("last_observed_result", "")
    ).strip()
    return (
        last_run != previous_run
        or last_result != previous_result
        or (previous_state == "running" and state != "running")
    )
