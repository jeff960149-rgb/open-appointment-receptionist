def realtime_response_instructions(
    name: str,
    result: dict,
    *,
    test_mode: bool = False,
) -> str:
    """Return the single authoritative follow-up instruction for a tool result."""
    audience = "tester" if test_mode else "caller"

    if name == "request_staff_callback" or result.get("callback_type") == "staff":
        if result.get("saved") and result.get("question_id") and result.get("notification_sent"):
            guidance = (
                "Say exactly: 'I can't transfer you, but a staff member will call you back.' "
                "Use the caller's language. Stop and wait; do not add booking questions, "
                "a goodbye, the store number, or a callback time. Never tell them to call the spa again."
            )
        elif result.get("saved") and result.get("question_id"):
            guidance = (
                "Say: 'I can't transfer you. I've saved your request, but I couldn't notify staff just now.' "
                "Do not promise a callback or tell them to call the same number again."
            )
        else:
            guidance = (
                "Say: 'Sorry, I can't transfer you or arrange a callback right now.' "
                "Do not claim the request was saved or tell them to call the same number again."
            )
    elif name == "request_coupon_callback" or result.get("callback_type") == "coupon":
        if result.get("saved") and result.get("question_id"):
            guidance = (
                "Say exactly: 'A staff member will call you back shortly about your coupon.' "
                "If the caller is speaking Chinese, say exactly '優惠券的問題，店員稍後會回電給您。' "
                "Stop after that sentence. Do not add a question, goodbye, booking confirmation, "
                "or coupon terms. The coupon-related booking will be handled by staff."
            )
        else:
            guidance = (
                "Say you could not register the callback request right now. Do not claim "
                "a callback was arranged or that an appointment was booked."
            )
    elif name == "get_business_information":
        if result.get("topic") == "hours":
            if not result.get("hours_loaded"):
                guidance = (
                    "Briefly say you cannot confirm the opening hours right now. "
                    "Do not invent hours, list appointment slots, or ask for duration."
                )
            else:
                guidance = (
                    "FIRST answer the caller's exact hours question from periods: "
                    "opening time for 'when do you open', closing time for 'when do "
                    "you close', or the full range only if asked for hours. If closed "
                    "is true, say the store is closed that day. Use natural spoken "
                    "AM/PM times. These are business hours, not appointment openings. "
                    "Look at the FULL conversation: if the caller previously said "
                    "'I want to make an appointment' and booking is unfinished, "
                    "you MUST reply with TWO sentences: the hours answer, followed "
                    "by 'What kind of massage would you like?' when the type is "
                    "missing. Ask duration instead only if massage was already supplied. "
                    "For a standalone hours inquiry with no booking request, just "
                    "answer the hours question. Never narrate checking or invent a free slot."
                )
        elif result.get("topic") == "location":
            guidance = (
                "Answer the location question directly using the exact short wording "
                "returned by the tool. Do not narrate checking, restate the question, "
                "arrange a callback, or add unrelated details."
            )
        else:
            guidance = (
                f"Answer the {audience}'s exact question using only the returned "
                "information. When the result supplies exact answer wording, use it. "
                "Use one short sentence unless the question requires a short list. Do "
                "not read unrelated prices or policies, narrate checking, describe "
                "internal sources, save a known answer as unanswered, or arrange a callback."
            )
        guidance += (
            " Answer the latest customer question before continuing booking. "
            "Ask at most one follow-up, and never ask duration before the missing "
            "massage type. Do not volunteer duration choices unless requested."
        )
    elif name == "save_unanswered_question":
        if result.get("saved") and result.get("question_id"):
            guidance = (
                f"Briefly tell the {audience} you do not want to give incorrect "
                "information and a staff member will call back with the answer. Do not "
                "mention internal tools or the staff messaging bot."
            )
        else:
            guidance = (
                f"Say you do not have a reliable answer and a staff member can follow "
                f"up with the {audience}."
            )
    elif name in {"save_appointment_request", "save_unverified_appointment_request"}:
        if (
            result.get("saved")
            and result.get("status") == "confirmed"
            and result.get("request_id")
            and result.get("smiley_written") is True
        ):
            guidance = (
                "Say exactly one short sentence: 'Your appointment is booked.' Never "
                "mention the confirmation number or add a welcome, farewell, repeated "
                "appointment details, or staff-confirmation language."
            )
        elif (
            result.get("saved")
            and result.get("request_id")
        ):
            guidance = (
                f"Tell the {audience} their preferred appointment details were recorded "
                "for staff review, but final booking is not confirmed yet. Do not "
                "say it is booked or confirmed. Say staff will follow up to confirm. "
                "Never speak an internal request ID or number."
            )
        else:
            guidance = (
                "Do not say you cannot book or tell the caller to contact the spa. "
                "If the requested time just became unavailable, briefly say so, recheck "
                "availability, and immediately offer another available time. Do not claim "
                "success unless Smiley saved and verified the appointment."
            )
    elif name == "check_smiley_availability":
        if result.get("schedule_loaded") is False:
            guidance = (
                "Say you cannot confirm the exact requested time right now. "
                "Do not claim it is available or fully booked, and do not proceed "
                "to the massage question as if the time were accepted. "
            )
        elif not result.get("available"):
            guidance = (
                "Say naturally: 'Sorry, we don't have that time available.' "
                "If the result explicitly confirms a booking conflict, say instead: "
                "'Sorry, that time is already booked.' If it specifies a past time or "
                "another reason, briefly explain that reason without inventing a booking. "
                "Never say 'The exact requested time is unavailable.' "
                "Then ask 'What other time works for you?' Do not ask about massage yet. "
            )
        elif result.get("availability_scope") == "start_time":
            guidance = (
                "FIRST say the exact requested time is possible as a start, for example "
                "'Yes, we can start at 10 a.m.' using the actual requested time. "
                "THEN ask 'What kind of massage would you like?' if not already supplied; "
                "otherwise ask only for the missing duration. Do not assume a duration "
                "or claim the full treatment fits or is booked. Recheck availability "
                "with the selected duration before readback or saving. "
            )
        else:
            guidance = (
                "First answer whether the exact requested time fits the requested duration. "
                "Then ask only for the next missing booking detail, if any. "
                "If the full name is missing, ask exactly 'What is your full name please?' "
            )
        guidance += (
            "Do not narrate checking. Use at most two short sentences. "
            "Do not mention suggested times or volunteer an "
            f"alternative unless the {audience} asked for options. If they challenge an "
            "earlier answer, treat this as a fresh recheck and do not argue."
        )
    elif name == "list_smiley_openings":
        if result.get("schedule_loaded") is False:
            guidance = "Say you cannot confirm available times right now; do not invent slots or ask for duration."
        elif not result.get("openings"):
            guidance = "Say there are no available starts for the requested day and checked duration, then ask which other day works."
        else:
            guidance = (
                "FIRST give at most three distinct start times from openings in one "
                "short sentence. Convert 24-hour values to natural AM/PM speech "
                "(13:00 is 1 p.m.). These are appointment options, not store opening hours. "
                "THEN ask 'What kind of massage would you like?' if missing; if already "
                "supplied, ask only the missing duration; if both are known, ask which "
                "start time works. Never demand duration before giving the available "
                "starts. For availability_scope=start_time, these are possible starts "
                "only: recheck the chosen start with the actual duration before booking. "
                "Do not volunteer 60/90-minute options unless asked. Do not narrate "
                "checking or mention the tool, cache, end times, or every opening."
            )
    else:
        guidance = (
            f"Explain the result briefly in the {audience}'s language. If schedule data "
            "is unavailable, do not suggest another time. Mention only returned options "
            "and never reveal internal therapist allocation."
        )

    if test_mode and result.get("test_mode") is True:
        guidance += " This is a dry-run; do not mention that unless the tester asks."
    return guidance
