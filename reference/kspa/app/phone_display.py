def format_line_phone(phone_number: str) -> str:
    """Show the full caller number to staff while keeping the last four obvious."""
    normalized = (phone_number or "").strip()
    if not normalized:
        return "未知"
    return f"{normalized}（末四碼 {normalized[-4:]}）"
