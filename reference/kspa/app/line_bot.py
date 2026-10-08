import base64
import hashlib
import hmac
import json
import logging
from collections import defaultdict
from datetime import datetime

import httpx

from app.phone_display import format_line_phone
from app.schedule_parser import normalize_schedule_records
from app.storage import (
    answer_unanswered_question,
    cancel_appointment,
    confirm_staged_schedule,
    discard_staged_schedule,
    edit_appointment,
    get_unanswered_question,
    get_call_transcript,
    get_appointment_notification_target,
    ignore_unanswered_question,
    list_appointments,
    list_recent_calls,
    list_unanswered_questions,
    list_schedule,
    set_appointment_notification_target,
    stage_schedule,
    update_appointment_status,
)


logger = logging.getLogger("ai-receptionist.line")
LINE_API_BASE = "https://api.line.me/v2/bot"
LINE_DATA_API_BASE = "https://api-data.line.me/v2/bot"


def verify_line_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    if not signature or not channel_secret:
        return False
    digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


async def _line_request(
    method: str,
    path: str,
    access_token: str,
    *,
    json_body: dict | None = None,
    base_url: str = LINE_API_BASE,
) -> httpx.Response:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            json=json_body,
        )
    if response.is_error:
        details = " ".join(response.text.split())[:1000]
        logger.error(
            "LINE API request failed: method=%s path=%s status=%s body=%s",
            method,
            path,
            response.status_code,
            details,
        )
    response.raise_for_status()
    return response


async def get_message_quota(access_token: str) -> dict:
    """Return LINE's current monthly message allowance and approximate usage."""
    quota = await _line_request("GET", "/message/quota", access_token)
    usage = await _line_request("GET", "/message/quota/consumption", access_token)
    return {"quota": quota.json(), "usage": usage.json()}


async def reply_text(reply_token: str, text: str, access_token: str) -> None:
    await _line_request(
        "POST",
        "/message/reply",
        access_token,
        json_body={
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
    )


async def push_text(user_id: str, text: str, access_token: str) -> None:
    await _line_request(
        "POST",
        "/message/push",
        access_token,
        json_body={
            "to": user_id,
            "messages": [{"type": "text", "text": text[:5000]}],
        },
    )


async def get_message_content(message_id: str, access_token: str) -> tuple[bytes, str]:
    response = await _line_request(
        "GET",
        f"/message/{message_id}/content",
        access_token,
        base_url=LINE_DATA_API_BASE,
    )
    return response.content, response.headers.get("content-type", "image/jpeg").split(";")[0]


def _schedule_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "document_type": {
                "type": "string",
                "enum": ["availability_schedule", "service_log"],
            },
            "document_date": {"type": "string"},
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "date": {"type": "string"},
                        "therapist": {"type": "string"},
                        "start_time": {"type": "string"},
                        "end_time": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["available", "unavailable"],
                        },
                    },
                    "required": [
                        "date",
                        "therapist",
                        "start_time",
                        "end_time",
                        "status",
                    ],
                    "additionalProperties": False,
                },
            },
            "service_records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "employee": {"type": "string"},
                        "item_code": {
                            "type": "string",
                            "enum": ["B", "F", "CB", "UNKNOWN"],
                        },
                        "service_name": {
                            "type": "string",
                            "enum": [
                                "Deep Tissue Full Body Massage",
                                "Foot Massage",
                                "Combination Body Massage",
                                "Unknown",
                            ],
                        },
                        "start_time": {"type": "string"},
                        "end_time": {"type": "string"},
                    },
                    "required": [
                        "employee", "item_code", "service_name",
                        "start_time", "end_time",
                    ],
                    "additionalProperties": False,
                },
            },
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "document_type", "document_date", "rows", "service_records", "warnings"
        ],
        "additionalProperties": False,
    }


def _schedule_extraction_prompt() -> str:
    local_date = datetime.now().date().isoformat()
    return (
        "Classify and extract a Footprint Massage paper form. There are two "
        "possible document types. An availability_schedule directly states when "
        "therapists are free. A service_log/booking sheet lists occupied appointment "
        "times under each named employee, usually with columns for employee name, "
        "item, time, payment, tips, and settlement. The listed times are NOT free; "
        "they are booked. Empty time gaps for an employee named on the sheet can be "
        "calculated as availability later by the backend. Do not create an employee "
        "from a completely blank section. For an availability_schedule, return one row "
        "per continuous availability interval in rows and leave service_records empty. "
        "For a service_log, leave rows empty and return only employee, item code, service "
        "name, start time, and end time in service_records; ignore all money, tip, card, "
        "and settlement columns. A completely blank fresh daily Footprint form is a "
        "service_log with no service_records; still extract its header date. The backend "
        "will apply the regular shifts: Former Staff and Staff B 10:00-22:00, and Staff A "
        "18:30-22:00. Service codes are exactly: B = Deep Tissue Full Body "
        "Massage; F = Foot Massage; CB = Combination Body Massage. Read the item only "
        "from the 项目/item column. A number attached before B, such as 90B or 120B, is "
        "a duration annotation and item_code is still B; it is never CB unless the "
        "letters CB are actually written. Be especially careful not to confuse "
        "handwritten B with F. Use the sequence of rows under each employee to resolve "
        "ambiguous clock digits: times should normally progress forward through that "
        "employee's day. Known employee names are Staff A, Former Staff, and Staff B. Normalize a "
        "clearly intended handwritten 'Ana' to Staff B, but never guess a genuinely "
        "unreadable name. Read the exact date written in the header. Do not replace a "
        "visible date with the current date. Use the current date only to supply a "
        "missing year. Use YYYY-MM-DD dates and 24-hour HH:MM times. If AM/PM is not "
        "printed, infer only from chronological order and store hours (10:00 through "
        "22:00); report every uncertain field in warnings. The current date is "
        f"{local_date}. Return only data matching the supplied JSON schema."
    )


def _gemini_schedule_schema() -> dict:
    """Gemini accepts JSON Schema, but not every OpenAI schema keyword."""
    schema = json.loads(json.dumps(_schedule_schema()))

    def remove_unsupported(value: object) -> None:
        if isinstance(value, dict):
            value.pop("additionalProperties", None)
            for child in value.values():
                remove_unsupported(child)
        elif isinstance(value, list):
            for child in value:
                remove_unsupported(child)

    remove_unsupported(schema)
    return schema


async def extract_schedule_with_gemini(
    image_bytes: bytes,
    mime_type: str,
    gemini_api_key: str,
    gemini_model: str,
) -> dict:
    payload = {
        "contents": [{
            "role": "user",
            "parts": [
                {"text": _schedule_extraction_prompt()},
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                },
            ],
        }],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseJsonSchema": _gemini_schedule_schema(),
        },
    }
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gemini_model}:generateContent"
    )
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            url,
            headers={"x-goog-api-key": gemini_api_key},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    output_text = "".join(
        part.get("text", "")
        for candidate in data.get("candidates", [])
        for part in candidate.get("content", {}).get("parts", [])
    )
    if not output_text:
        raise ValueError("Gemini returned no schedule data.")
    return json.loads(output_text)


async def extract_schedule_from_image(
    image_bytes: bytes,
    mime_type: str,
    openai_api_key: str,
    vision_model: str,
) -> dict:
    image_url = (
        f"data:{mime_type};base64," + base64.b64encode(image_bytes).decode("ascii")
    )
    payload = {
        "model": vision_model,
        "store": False,
        "instructions": _schedule_extraction_prompt(),
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Read this schedule photo."},
                    {"type": "input_image", "image_url": image_url, "detail": "high"},
                ],
            }
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "massage_schedule",
                "strict": True,
                "schema": _schedule_schema(),
            }
        },
    }
    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {openai_api_key}"},
            json=payload,
        )
    response.raise_for_status()
    data = response.json()
    output_text = "".join(
        part.get("text", "")
        for item in data.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    )
    if not output_text:
        raise ValueError("OpenAI returned no schedule data.")
    return json.loads(output_text)


def format_schedule_preview(rows: list[dict[str, str]], warnings: list[str]) -> str:
    lines = [f"辨认到 {len(rows)} 笔班表："]
    lines.extend(
        f"{row['date']} | {row['therapist']} | {row['start_time']}-{row['end_time']} | {row['status']}"
        for row in rows[:30]
    )
    if len(rows) > 30:
        lines.append(f"……另外还有 {len(rows) - 30} 笔")
    if warnings:
        lines.append("\n需要人工检查：")
        lines.extend(f"- {warning}" for warning in warnings[:10])
    lines.append("\n確認無誤請回覆：確認班表\n不要更新請回覆：取消班表")
    return "\n".join(lines)


def format_service_log_preview(
    document_date: str,
    records: list[dict[str, str]],
    available_rows: list[dict[str, str]],
    warnings: list[str],
) -> str:
    lines = [f"辨认到已预约纪录（{document_date or '日期不清楚'}），共 {len(records)} 笔："]
    lines.extend(
        f"{record['employee']} | {record['item_code']} | "
        f"{record['service_name']} | {record['start_time']}-{record['end_time']}"
        for record in records[:40]
    )
    if warnings:
        lines.append("\n需要人工检查：")
        lines.extend(f"- {warning}" for warning in warnings[:10])
    lines.append("\n根据以上占用时段算出的可预约空档：")
    lines.extend(
        f"{row['therapist']} | {row['start_time']}-{row['end_time']}"
        for row in available_rows[:40]
    )
    lines.append("\n请先人工核对日期、员工和时间。正确请回覆：確認班表\n不正确请回覆：取消班表")
    return "\n".join(lines)


def derive_availability_from_bookings(
    document_date: str,
    records: list[dict[str, str]],
    opening_time: str = "10:00",
    closing_time: str = "22:00",
) -> list[dict[str, str]]:
    """Return free gaps for employees actually named on a booking sheet."""
    datetime.strptime(document_date, "%Y-%m-%d")

    def minutes(value: str) -> int:
        parsed = datetime.strptime(value, "%H:%M")
        return parsed.hour * 60 + parsed.minute

    def clock(value: int) -> str:
        return f"{value // 60:02d}:{value % 60:02d}"

    opening = minutes(opening_time)
    closing = minutes(closing_time)
    staff_shifts = {
        "former staff": ("Former Staff", minutes("10:00"), minutes("22:00")),
        "staff b": ("Staff B", minutes("10:00"), minutes("22:00")),
        "staff a": ("Staff A", minutes("18:30"), minutes("22:00")),
    }
    bookings_by_employee: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for record in records:
        employee = record.get("employee", "").strip()
        if not employee:
            continue
        start = minutes(record["start_time"])
        end = minutes(record["end_time"])
        if end <= start:
            raise ValueError(f"Invalid booking time for {employee}: {record['start_time']}-{record['end_time']}")
        key = employee.casefold()
        if key not in staff_shifts:
            continue
        _, shift_start, shift_end = staff_shifts[key]
        bookings_by_employee[key].append((max(start, shift_start), min(end, shift_end)))

    availability: list[dict[str, str]] = []
    for key, (display_name, shift_start, shift_end) in staff_shifts.items():
        bookings = bookings_by_employee[key]
        cursor = shift_start
        for start, end in sorted(bookings):
            if end <= shift_start or start >= shift_end:
                continue
            if start > cursor:
                availability.append({
                    "date": document_date,
                    "therapist": display_name,
                    "start_time": clock(cursor),
                    "end_time": clock(start),
                    "status": "available",
                })
            cursor = max(cursor, end)
        if cursor < shift_end:
            availability.append({
                "date": document_date,
                "therapist": display_name,
                "start_time": clock(cursor),
                "end_time": clock(shift_end),
                "status": "available",
            })
    return availability


async def process_schedule_image(
    *,
    conversation_id: str,
    message_id: str,
    line_access_token: str,
    openai_api_key: str,
    vision_model: str,
    gemini_api_key: str = "",
    gemini_model: str = "gemini-3.6-flash",
) -> None:
    try:
        image_bytes, mime_type = await get_message_content(message_id, line_access_token)
        if gemini_api_key:
            try:
                extracted = await extract_schedule_with_gemini(
                    image_bytes, mime_type, gemini_api_key, gemini_model
                )
                logger.info("Schedule image extracted with Gemini model %s", gemini_model)
            except Exception:
                logger.exception("Gemini schedule extraction failed; trying OpenAI fallback")
                if not openai_api_key:
                    raise
                extracted = await extract_schedule_from_image(
                    image_bytes, mime_type, openai_api_key, vision_model
                )
        else:
            extracted = await extract_schedule_from_image(
                image_bytes, mime_type, openai_api_key, vision_model
            )
        if extracted.get("document_type") == "service_log":
            available_rows = derive_availability_from_bookings(
                extracted.get("document_date", ""),
                extracted.get("service_records", []),
            )
            if not available_rows:
                raise ValueError("No reliable availability gaps could be calculated.")
            stage_schedule(conversation_id, available_rows)
            await push_text(
                conversation_id,
                format_service_log_preview(
                    extracted.get("document_date", ""),
                    extracted.get("service_records", []),
                    available_rows,
                    extracted.get("warnings", []),
                ),
                line_access_token,
            )
            return
        rows = normalize_schedule_records(extracted.get("rows", []))
        if not rows:
            await push_text(
                conversation_id,
                "我没有辨认到可靠的班表资料。请拍正一点、靠近一点，再传一次。",
                line_access_token,
            )
            return
        stage_schedule(conversation_id, rows)
        await push_text(
            conversation_id,
            format_schedule_preview(rows, extracted.get("warnings", [])),
            line_access_token,
        )
    except Exception as exc:
        logger.exception("LINE schedule image processing failed")
        await push_text(
            conversation_id,
            f"班表照片处理失败：{type(exc).__name__}。请检查后端日志。",
            line_access_token,
        )


def handle_admin_text(user_id: str, text: str, *, conversation_id: str = "") -> str:
    normalized = text.strip()
    schedule_owner_id = conversation_id or user_id
    if normalized in {"設定預約通知群", "设定预约通知群", "设置预约通知群"}:
        if not conversation_id or conversation_id == user_id:
            return "请在要接收所有通知的 LINE 群组里发送这条指令。"
        set_appointment_notification_target(conversation_id)
        return "设置完成。之后所有主动通知只会发送到这个群组，不会私讯管理员。"
    if normalized in {"取消預約通知群", "取消预约通知群"}:
        set_appointment_notification_target(None)
        return "已取消通知群。设置新群组前，系统不会主动发送 LINE 通知。"
    if normalized in {"查看預約通知群", "查看预约通知群"}:
        target = get_appointment_notification_target()
        return "LINE 主动通知群已设置。" if target else "目前未设置 LINE 主动通知群。"
    if normalized in {"確認班表", "确认班表"}:
        count = confirm_staged_schedule(schedule_owner_id)
        return f"已更新正式班表，共 {count} 笔。" if count else "没有等待确认的班表。"
    if normalized in {"取消班表"}:
        discard_staged_schedule(schedule_owner_id)
        return "已取消这次班表更新。"
    if normalized in {"班表", "查看班表", "檢視班表"}:
        rows = list_schedule()
        if not rows:
            return "目前没有正式班表。"
        return "目前班表：\n" + "\n".join(
            f"{row['date']} | {row['therapist']} | {row['start_time']}-{row['end_time']}"
            for row in rows[:30]
        )
    if normalized in {"預約", "预约", "待確認預約", "待确认预约"}:
        rows = [
            row for row in list_appointments()
            if row["status"] in {"pending", "needs_review"}
        ]
        if not rows:
            return "目前没有等待确认的预约。"
        return "待确认预约：\n" + "\n".join(
            f"#{row['id']} {row['customer_name']} | {row['requested_date']} "
            f"{row['requested_time']} | {row['service']} | "
            f"{row['therapist'] or '未指定'} | {row['status']}"
            for row in rows[:20]
        )
    if normalized in {"最近預約", "最近预约", "查看預約", "查看预约"}:
        rows = list_appointments()
        if not rows:
            return "目前没有预约记录。"
        return "最近预约：\n" + "\n".join(
            f"#{row['id']} {row['customer_name']} | {row['requested_date']} "
            f"{row['requested_time']} | {row['service']} {row['duration_minutes']}分钟 | "
            f"{row['therapist'] or '未指定'} | {row['status']}"
            for row in rows[:20]
        )
    if normalized in {"最近通話", "最近通话", "通話紀錄", "通话记录"}:
        rows = list_recent_calls()
        if not rows:
            return "目前没有通话文字记录。"
        return "最近通话（保留48小时）：\n" + "\n".join(
            f"#{row['stream_sid'][-8:]} | 来电 {format_line_phone(row['caller_phone'])} "
            f"| {row['started_at'][:16].replace('T', ' ')} | {row['transcript_count']}句"
            for row in rows
        ) + "\n\n查看范例：查看通话 12345678"
    for prefix in ("查看通話", "查看通话"):
        if normalized.startswith(prefix):
            call_id = normalized.removeprefix(prefix).strip().lstrip("#")
            if not call_id:
                return f"格式错误。范例：{prefix} 12345678"
            rows = get_call_transcript(call_id)
            if not rows:
                return f"找不到通话 #{call_id} 的文字记录。"
            return f"通话 #{call_id}：\n" + "\n".join(
                f"{'客人' if row['speaker'] == 'customer' else 'Kiki'}：{row['text']}"
                for row in rows
            )
    if normalized in {"待補充問題", "待补充问题"}:
        rows = list_unanswered_questions()
        if not rows:
            return "目前没有待补充问题。"
        return "待补充问题：\n" + "\n".join(
            f"#{row['id']} {row['question']}\n来电：{format_line_phone(row['caller_phone'])}"
            for row in rows[:20]
        ) + "\n\n回复范例：回答 15 正确答案"
    for prefix in ("查看問題", "查看问题"):
        if normalized.startswith(prefix):
            try:
                question_id = int(normalized.removeprefix(prefix).strip().lstrip("#"))
            except ValueError:
                return f"格式错误。范例：{prefix} 15"
            row = get_unanswered_question(question_id)
            if not row:
                return f"找不到问题 #{question_id}。"
            return (
                f"问题 #{row['id']}\n"
                f"状态：{row['status']}\n"
                f"来电：{format_line_phone(row['caller_phone'])}\n"
                f"问题：{row['question']}\n"
                f"背景：{row['context'] or '无'}\n"
                f"答案：{row['answer'] or '尚未回答'}"
            )
    for prefix in ("回答",):
        if normalized.startswith(prefix):
            remainder = normalized.removeprefix(prefix).strip()
            parts = remainder.split(maxsplit=1)
            if len(parts) != 2:
                return "格式错误。范例：回答 15 正确答案"
            try:
                question_id = int(parts[0].lstrip("#"))
            except ValueError:
                return "格式错误。范例：回答 15 正确答案"
            if not answer_unanswered_question(question_id, parts[1]):
                return f"找不到问题 #{question_id}。"
            return f"问题 #{question_id} 已回答，并加入 AI 店铺知识库。"
    for prefix in ("忽略問題", "忽略问题"):
        if normalized.startswith(prefix):
            try:
                question_id = int(normalized.removeprefix(prefix).strip().lstrip("#"))
            except ValueError:
                return f"格式错误。范例：{prefix} 15"
            return (
                f"问题 #{question_id} 已忽略。"
                if ignore_unanswered_question(question_id)
                else f"找不到待处理问题 #{question_id}。"
            )
    for prefix, status in (
        ("確認預約", "confirmed"), ("确认预约", "confirmed"),
        ("拒絕預約", "rejected"), ("拒绝预约", "rejected"),
    ):
        if normalized.startswith(prefix):
            try:
                appointment_id = int(normalized.removeprefix(prefix).strip().lstrip("#"))
            except ValueError:
                return f"格式错误。范例：{prefix} 12"
            update_appointment_status(appointment_id, status)
            return f"预约 #{appointment_id} 已标记为 {status}。"
    for prefix in ("取消預約", "取消预约"):
        if normalized.startswith(prefix):
            try:
                appointment_id = int(normalized.removeprefix(prefix).strip().lstrip("#"))
            except ValueError:
                return f"格式错误。范例：{prefix} 12"
            result = cancel_appointment(appointment_id)
            if not result.get("cancelled"):
                return f"找不到预约 #{appointment_id}。"
            return f"预约 #{appointment_id} 已取消，该时段已重新开放。"
    for prefix in ("修改預約", "修改预约"):
        if normalized.startswith(prefix):
            parts = normalized.removeprefix(prefix).strip().split(maxsplit=2)
            if len(parts) != 3:
                return f"格式错误。范例：{prefix} 12 时间 15:30"
            try:
                appointment_id = int(parts[0].lstrip("#"))
            except ValueError:
                return f"格式错误。范例：{prefix} 12 时间 15:30"
            field_names = {
                "日期": "requested_date", "date": "requested_date",
                "時間": "requested_time", "时间": "requested_time", "time": "requested_time",
                "按摩師": "therapist_preference", "按摩师": "therapist_preference",
                "服務": "service", "服务": "service", "service": "service",
                "時長": "duration_minutes", "时长": "duration_minutes", "duration": "duration_minutes",
            }
            field = field_names.get(parts[1].casefold())
            if not field:
                return "可修改：日期、时间、按摩师、服务、时长。"
            value: object = parts[2]
            if field == "duration_minutes":
                try:
                    value = int(parts[2].removesuffix("分钟").strip())
                except ValueError:
                    return "时长必须是分钟数，例如：修改预约 12 时长 60"
            result = edit_appointment(appointment_id, **{field: value})
            if not result.get("updated"):
                availability = result.get("availability") or {}
                suggestions = availability.get("suggested_times") or []
                suggestion_text = ""
                if suggestions:
                    suggestion_text = "\n可用选择：" + "、".join(
                        f"{slot['therapist']} {slot['start_time']}" for slot in suggestions[:5]
                    )
                return f"无法修改预约 #{appointment_id}：{result.get('reason')}。{suggestion_text}"
            row = result["appointment"]
            return (
                f"预约 #{appointment_id} 已修改：{row['requested_date']} "
                f"{row['requested_time']}，{row['therapist']}，"
                f"{row['service']} {row['duration_minutes']}分钟。"
            )
    return (
        "可以传送纸本班表照片，或使用：\n"
        "確認班表 / 取消班表\n"
        "查看班表\n"
        "待確認預約\n"
        "最近預約 / 取消預約 12\n"
        "修改預約 12 時間 15:30\n"
        "確認預約 12 / 拒絕預約 12\n"
        "待補充問題 / 查看問題 15\n"
        "回答 15 正确答案 / 忽略問題 15"
    )
