OTHER_SERVICES_REPLY = (
    "We offer Deep Tissue, Swedish, and Signature Massage. We also have face care."
)

FIRST_TIME_DISCOUNT_REPLY = (
    "If you're a first-time visitor, you get $30 off. If two people come together "
    "and both are first-time visitors, each person gets $30 off."
)

KSPA_TEMPORARY_CAPACITY_OVERRIDES = {
    "2026-09-06": {"start_time": "15:00", "capacity": 3},
}


KSPA_BUSINESS_DETAILS = {
    "location": (
        "K Spa and Bodywork is at 123 Example Street, Example City, California 00000. "
        "The store phone number is 202-555-0123. When asked where the store is, "
        "say: 'We're at 123 Example Street in Example City.' Give the ZIP code only if useful."
    ),
    "services": (
        "When the caller has not asked for other services or named another massage type, introduce only "
        "Deep Tissue Massage and Swedish Massage. Quote only these member prices. "
        "Swedish Massage is $70 for 30 minutes, $80 for 45 minutes, $90 for "
        "60 minutes, and $140 for 90 minutes. Swedish three-session packages "
        "are valid for 90 days: $255 for three 60-minute sessions or $405 for "
        "three 90-minute sessions. Deep Tissue Massage is $80 for 30 minutes, "
        "$90 for 45 minutes, $100 for 60 minutes, and $150 for 90 minutes. "
        "Deep Tissue three-session packages are valid for 90 days: $285 for "
        "three 60-minute sessions or $435 for three 90-minute sessions. "
        "Health Massage is $150 for 60 minutes or $200 for 90 minutes. Health "
        "Massage three-session packages are valid for 90 days: $450 for three "
        "60-minute sessions or $570 for three 90-minute sessions. Mention "
        "Health Massage only when the caller asks about it or asks for the full menu. "
        "Additional items are Relieve Pain for $60, Hot Stones for $20, CBD Oil "
        "for $10, and Cupping for $40; mention them only when asked. Membership "
        "is free. Never quote a non-member, regular, or original price. "
        "Signature Massage combines massage with hot stones placed on specific "
        "areas of the body. It is $130 for 60 minutes, $160 for 75 minutes, and "
        "$180 for 90 minutes. "
        "Mention "
        "it only when the caller specifically asks about "
        "Signature Massage or asks about other services. Couple Massage is available for 60, 75, or 90 "
        "minutes, but mention it only when the caller specifically asks about "
        "a couple massage or the full menu. Facial and skin-care services are Hydrating and "
        "Moisturizing Skin Care, Deeply Hydrating and Moisturizing Skin Care, "
        "and a Massage and Skin Care Package with a one-hour aromatherapy "
        "massage and a 30-minute facial. Mention facial or skin-care services "
        "when the caller asks about face, facial, skin-care, other services, or the full menu. "
        f"For 'Do you have any other services?', say briefly: '{OTHER_SERVICES_REPLY}' "
        "Do not read prices "
        "or every duration unless asked. If they ask for the full menu, also briefly "
        "name Health Massage, Couple Massage, and the Massage and Skin Care Package. "
        "Relaxation Massage is deleted and must never be offered. "
        "Prices for services other than the listed Swedish, Deep Tissue, Health "
        "Massage, Signature Massage, packages, and additional items are not "
        "verified; never guess them. "
        "Only when the caller asks about the first-time-visitor discount or a $30 "
        f"discount coupon, answer directly: '{FIRST_TIME_DISCOUNT_REPLY}' The discount "
        "applies separately to every person who is "
        "visiting K Spa for the first time, including larger parties. Never tell the "
        "caller to bring, show, or present a physical coupon. "
        "This is a verified policy: never defer this question to staff, save it as "
        "unanswered, or arrange a callback. Do not volunteer the discount or invent "
        "an expiration date, minimum purchase, or any other condition."
    ),
}
