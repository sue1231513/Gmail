import asyncio
import base64
import json
import os
import re
import sqlite3
from datetime import datetime
from email.mime.text import MIMEText
 
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP
 
# ─── 初始化 MCP ──────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 8000))
mcp = FastMCP("gmail-mcp", host="0.0.0.0", port=PORT)
 
 
# ─── Gmail 认证 ───────────────────────────────────────────────
def _get_gmail_service():
    token_json = os.environ.get("GOOGLE_USER_TOKEN_JSON")
    if not token_json:
        return None
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return build("gmail", "v1", credentials=creds)
    except Exception:
        return None
 
 
# ─── 记忆数据库 (SQLite) ──────────────────────────────────────
DB_PATH = os.environ.get("MEMORY_DB_PATH", "/data/memory.db")
 
def _init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            title    TEXT,
            content  TEXT,
            category TEXT,
            emotion  TEXT,
            tag      TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()
 
def _save_memory_to_db(title: str, content: str, category: str, emotion: str, tag: str):
    try:
        _init_db()
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO memories (title, content, category, emotion, tag, created_at) VALUES (?,?,?,?,?,?)",
            (title, content, category, emotion, tag, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass  # 记忆写入失败不影响主流程
 
 
# ─── 邮件正文解析 ──────────────────────────────────────────────
def _parse_gmail_body(payload: dict) -> str:
    """递归提取 Gmail payload，优先取 text/plain"""
    mime_type = payload.get("mimeType", "")
 
    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
 
    if mime_type == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
 
    for part in payload.get("parts", []):
        result = _parse_gmail_body(part)
        if result:
            return result
 
    return ""
 
 
def _clean_email_body(raw: str) -> str:
    """去除 HTML 标签和多余空白"""
    clean = re.sub(r"<[^>]+>", "", raw)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    clean = re.sub(r"[ \t]+", " ", clean)
    return clean.strip()
 
 
# ─── MCP Tools ───────────────────────────────────────────────
 
@mcp.tool()
async def send_email_via_api(subject: str, content: str):
    """⚠️这个只能发给管理员（你自己），绝对不能用来回复别人的邮件！要回邮件用 reply_external_email！"""
    to_email = os.environ.get("ADMIN_EMAIL", "")
    if not to_email:
        return "❌ 未配置 ADMIN_EMAIL 环境变量。"
 
    service = _get_gmail_service()
    if not service:
        return "❌ Gmail 认证失败，请检查 GOOGLE_USER_TOKEN_JSON。"
 
    def _send():
        message = MIMEText(content)
        message["to"] = to_email
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
 
    await asyncio.to_thread(_send)
    return f"✅ 内部邮件已发送至管理员。"
 
 
@mcp.tool()
async def check_inbox(max_results: int = 15, query: str = "label:INBOX"):
    """打开信箱看看有没有新邮件，未读的会带正文预览。"""
    try:
        service = await asyncio.to_thread(_get_gmail_service)
        if not service:
            return "❌ Gmail 认证失败，请检查 GOOGLE_USER_TOKEN_JSON。"
 
        def _fetch_gmail():
            results = service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
            messages = results.get("messages", [])
            if not messages:
                return "📭 信箱空空如也。"
 
            unread_list = []
            read_list = []
            preview_count = 0
 
            for msg in messages:
                m_meta = service.users().messages().get(
                    userId="me", id=msg["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"]
                ).execute()
                headers = m_meta.get("payload", {}).get("headers", [])
                subject  = next((h["value"] for h in headers if h["name"] == "Subject"), "无标题")
                sender   = next((h["value"] for h in headers if h["name"] == "From"),    "未知")
                date_str = next((h["value"] for h in headers if h["name"] == "Date"),    "未知时间")
                labels   = m_meta.get("labelIds", [])
                is_unread = "UNREAD" in labels
 
                if is_unread:
                    if preview_count < 5:
                        m_full = service.users().messages().get(userId="me", id=msg["id"], format="full").execute()
                        raw_body   = _parse_gmail_body(m_full.get("payload", {}))
                        clean_body = _clean_email_body(raw_body)
                        body_preview = clean_body.strip()[:1000] if clean_body else m_full.get("snippet", "")
                        unread_list.append(
                            f"🆔 ID: {msg['id']}\n📅 时间: {date_str}\n👤 来自: {sender}\n"
                            f"📧 标题: {subject}\n🏷️ 状态: 🆕 [未读/待回复]\n📝 正文预览: {body_preview}\n"
                        )
                        preview_count += 1
                    else:
                        unread_list.append(
                            f"🆔 ID: {msg['id']} | 📅 时间: {date_str} | 👤 来自: {sender} | "
                            f"📧 标题: {subject} | 🏷️ 状态: 🆕 [未读/待回复 - 额度满未抓取正文]"
                        )
                else:
                    read_list.append(
                        f"🆔 ID: {msg['id']} | 📅 时间: {date_str} | 👤 来自: {sender} | "
                        f"📧 标题: {subject} | 🏷️ 状态: ✅ [已读/已处理]"
                    )
 
            final_output  = "🚨 【紧急待处理邮件 (最新5封包含正文)】\n"
            final_output += "\n".join(unread_list) if unread_list else "没有待处理的新邮件。\n"
            final_output += "\n\n🗄️ 【已读/已回复邮件归档 (仅展示摘要)】\n"
            final_output += "\n".join(read_list) if read_list else "没有已处理的邮件。\n"
            return final_output
 
        content = await asyncio.to_thread(_fetch_gmail)
        return f"📬 【原生信箱状态】\n\n{content}"
    except Exception as e:
        return f"❌ Gmail 读取失败: {e}"
 
 
@mcp.tool()
async def read_full_email(message_id: str):
    """读完一封邮件的完整正文。在收件箱里看到正文被截断的时候用这个看全的。"""
    try:
        service = await asyncio.to_thread(_get_gmail_service)
        if not service:
            return "❌ Gmail 认证失败。"
 
        def _read_single():
            m = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            headers  = m.get("payload", {}).get("headers", [])
            subject  = next((h["value"] for h in headers if h["name"] == "Subject"), "无标题")
            sender   = next((h["value"] for h in headers if h["name"] == "From"),    "未知")
            raw_body = _parse_gmail_body(m.get("payload", {}))
            full_text = _clean_email_body(raw_body).strip() if raw_body else m.get("snippet", "无法解析正文内容")
            return subject, sender, full_text
 
        subject, sender, full_text = await asyncio.to_thread(_read_single)
        await asyncio.to_thread(
            _save_memory_to_db,
            "📧 查阅邮件",
            f"发件人: {sender}\n标题: {subject}\n正文: {full_text[:300]}...",
            "流水", "平静", "Email_Process"
        )
        return f"📧 标题: {subject}\n👤 发件人: {sender}\n\n📄 完整正文:\n{full_text}"
    except Exception as e:
        return f"❌ 读取单封邮件失败: {e}"
 
 
@mcp.tool()
async def reply_external_email(
    to_email: str,
    subject: str,
    content: str,
    thread_id: str = "",
    message_id: str = "",
):
    """给别人回邮件。传了 message_id 会自动把原邮件标成已读。"""
    try:
        service = await asyncio.to_thread(_get_gmail_service)
        if not service:
            return "❌ Gmail 认证失败。"
 
        def _send_gmail():
            message = MIMEText(content)
            message["to"]      = to_email
            message["subject"] = subject
            raw  = base64.urlsafe_b64encode(message.as_bytes()).decode()
            body = {"raw": raw}
            if thread_id:
                body["threadId"] = thread_id
 
            service.users().messages().send(userId="me", body=body).execute()
 
            if message_id:
                service.users().messages().modify(
                    userId="me",
                    id=message_id,
                    body={"removeLabelIds": ["UNREAD"]}
                ).execute()
 
        await asyncio.to_thread(_send_gmail)
        await asyncio.to_thread(
            _save_memory_to_db,
            "📧 原生回信",
            f"发给 {to_email}: {subject}\n正文: {content}",
            "流水", "认真", "Email_Process"
        )
        return f"✅ 邮件已通过原生接口发送至 {to_email}！"
    except Exception as e:
        return f"❌ 发送失败: {e}"
 
 
# ─── 启动 ─────────────────────────────────────────────────────
if __name__ == "__main__":
    mcp.run(transport="streamable-http")
