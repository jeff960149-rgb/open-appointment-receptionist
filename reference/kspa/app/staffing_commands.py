"""Explicit date/headcount commands, processed only after administrator auth."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.storage import get_daily_capacity, set_daily_capacity

DAY = r"(今天|明天|後天|后天|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[/月]\d{1,2}日?)"
STAFF = r"(?:按摩[師师]|[師师]傅)"
NUMBER = r"(\d+|[零〇一二兩两三四五六七八九十]+)"
HELP = "請說明日期及人數，例如：今天1人上班、今天兩人上班、明天按摩師只有一人。查詢可說：今天幾人上班。"


def _count(text: str) -> int:
    if text.isdecimal():
        return int(text)
    digits = dict(zip("零〇一二兩两三四五六七八九", (0, 0, 1, 2, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    if text.count("十") == 1:
        left, right = text.split("十")
        return (digits[left] if left else 1) * 10 + (digits[right] if right else 0)
    return digits[text]


def _date(text: str, now: datetime) -> str:
    offsets = {"今天": 0, "明天": 1, "後天": 2, "后天": 2}
    if text in offsets:
        return (now.date() + timedelta(days=offsets[text])).isoformat()
    parts = re.findall(r"\d+", text)
    if len(parts) == 2:
        parts.insert(0, str(now.year))
    return datetime(*map(int, parts)).date().isoformat()


def handle_capacity_text(text: str, actor: str, *, now_local: datetime | None = None) -> str | None:
    normalized = re.sub(r"\s+", "", text).strip("。！!.")
    now = now_local or datetime.now(ZoneInfo("America/Los_Angeles"))
    now = now.astimezone(ZoneInfo("America/Los_Angeles"))
    query = re.fullmatch(r"(?:查看|查詢|查询)?" + DAY + STAFF + r"(?:人[數数]|有幾人|有几人|有多少人)[？?]?", normalized)
    if not query:
        query = re.fullmatch(r"(?:查看|查詢|查询)?" + DAY + r"(?:有)?(?:幾|几|多少)人上班[？?]?", normalized)
    reset = re.fullmatch(DAY + r"(?:按摩[師师])?恢[復复](?:預設|预设|默認|默认)人[數数]", normalized)
    write = re.fullmatch(r"(?:請把|请把|把|設定|设定|设置)?" + DAY + r"(?:的)?" + STAFF
        + r"(?:人[數数])?(?:只有|僅有|仅有|有|是|為|为|改成|改為|改为|設定為|设定为|设置为)?"
        + NUMBER + r"(?:[個个位名]?人|位|名)?", normalized)
    if not write:
        write = re.fullmatch(DAY + r"(?:只有|僅有|仅有|有)" + NUMBER + r"[個个位名]?" + STAFF, normalized)
    if not write:
        write = re.fullmatch(DAY + r"(?:只有|有)?" + NUMBER + r"[個个]?人上班", normalized)
    zero = re.fullmatch(DAY + r"(?:沒有|没有)" + STAFF, normalized)
    match = query or reset or write or zero
    if not match:
        if "人上班" in normalized or re.search(STAFF + r".*(?:人[數数]|[零〇一二兩两三四五六七八九十\d]+[個个位名]?人)|恢[復复](?:預設|预设)人[數数]", normalized):
            return HELP
        return None
    try:
        day = _date(match[1], now)
        if query:
            setting = get_daily_capacity(day)
        else:
            count = None if reset else 0 if zero else _count(match[2])
            setting = set_daily_capacity(day, count, actor=actor, now_local=now)
    except (ValueError, KeyError):
        return "日期或人數不正確；請使用今天或未來的日期，人數為 0 到 20 的整數。"
    source = "指定人數" if setting["override"] else "預設人數"
    prefix = "" if query else "已儲存。"
    result = f"{prefix}{day} 按摩師 {setting['count']} 人（{source}，店家當地日期）。"
    if not query:
        result += ("當天停止接受新預約。" if setting["count"] == 0 else f"當天同時最多接受 {setting['count']} 位客人的預約。")
        result += "既有預約保留；其他日期不變。"
    return result
