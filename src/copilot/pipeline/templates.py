"""Fixed texts for blocked intents (template_reply). No LLM writes these: a hard-rule part of a
reply is always the same, reviewed wording.

DRAFT WORDING, to be replaced by Valetax's approved texts (support + compliance own them,
PRD FR-4.7). They promise nothing, decide nothing and give no timelines or fees.

Languages: English and Arabic (Modern Standard Arabic, PRD §8). Any other reply language gets
the English text and an agent note (`missing_language`); a person translates it. Phase 2: the
approved texts in every site language.
"""

TEMPLATES: dict[str, dict[str, str]] = {
    "decline_advice": {
        "en": "We're not able to give trading advice, recommendations or predictions, so we "
        "can't tell you what to trade or when. Any trading decision is yours to make.",
        "ar": "لا يمكننا تقديم نصائح أو توصيات أو توقعات تداول، لذلك لا نستطيع أن نحدد لك ماذا "
        "تتداول أو متى. قرار التداول يعود إليك وحدك.",
    },
    "ack_kyc": {
        "en": "Thank you for your message about your verification. Our verification team "
        "reviews every request and will contact you about the next steps.",
        "ar": "شكرًا لرسالتك بخصوص توثيق حسابك. يراجع فريق التوثيق لدينا كل طلب، وسيتواصل معك "
        "بخصوص الخطوات التالية.",
    },
    "ack_aml": {
        "en": "Thank you for your message. Your request has been passed to the responsible team, "
        "who will review it and contact you.",
        "ar": "شكرًا لرسالتك. تم تحويل طلبك إلى الفريق المختص، وسيقوم بمراجعته والتواصل معك.",
    },
    "ack_complaint": {
        "en": "Thank you for telling us about this, and we're sorry for the trouble. Your "
        "message has been passed to a senior member of our team, who will review it and get "
        "back to you.",
        "ar": "شكرًا لإبلاغنا بذلك، ونعتذر عن الإزعاج. تم تحويل رسالتك إلى أحد كبار أعضاء فريقنا، "
        "وسيقوم بمراجعتها والرد عليك.",
    },
    "ack_vulnerable": {
        "en": "Thank you for reaching out and telling us how you feel. A member of our team "
        "will personally get back to you as soon as possible.",
        "ar": "شكرًا لتواصلك معنا ومشاركتنا ما تشعر به. سيتواصل معك أحد أعضاء فريقنا شخصيًا في "
        "أقرب وقت ممكن.",
    },
    "ack_security": {
        "en": "Thank you for your message. For your security, requests about account access, "
        "contact details or security settings are handled through our secure verification "
        "process. A member of our team will contact you.",
        "ar": "شكرًا لرسالتك. حرصًا على أمان حسابك، تتم معالجة الطلبات المتعلقة بالدخول إلى الحساب "
        "أو بيانات التواصل أو إعدادات الأمان من خلال إجراءات التحقق الآمنة لدينا. سيتواصل معك "
        "أحد أعضاء فريقنا.",
    },
}


def render(key: str, language: str) -> tuple[str, bool]:
    """(text, translated?). Falls back to English when the language has no approved text."""
    texts = TEMPLATES[key]
    base = language.split("-")[0]
    if base in texts:
        return texts[base], True
    return texts["en"], False
