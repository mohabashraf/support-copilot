"""Fixed texts for blocked intents (template_reply). No LLM writes these: a hard-rule part of a
reply is always the same, reviewed wording.

DRAFT WORDING, to be replaced by Valetax's approved texts (support + compliance own them,
PRD FR-4.7). They promise nothing, decide nothing and give no timelines or fees.

Languages: English and Arabic (Modern Standard Arabic, PRD §8) for every template. The
trading-advice decline (`decline_advice`, the brief's "decline politely") is also in the other
site languages: id, ms, th, vi, zh-TW, ru, fr, pt, es. Kurdish (ckb) and Swahili (sw) are
low-resource: English by policy (PRD FR-5.1). A language with no text gets English + an agent
note; a machine-translated text gets a note too (REVIEW below). Phase 2: approved texts.
"""

# Who checked each text. Shown to the agent when a text that is not yet reviewed is used.
REVIEW = {
    "en": "draft wording, to be approved by Valetax support/compliance",
    "ar": "draft wording (MSA), pending native review",
    **{lang: "machine translation, pending native review"
       for lang in ("id", "ms", "th", "vi", "zh-TW", "ru", "fr", "pt", "es")},
}  # fmt: skip
ALIASES = {"zh": "zh-TW", "zh-Hant": "zh-TW"}  # the help site has Traditional Chinese only

TEMPLATES: dict[str, dict[str, str]] = {
    "decline_advice": {
        "en": "We're not able to give trading advice, recommendations or predictions, so we "
        "can't tell you what to trade or when. Any trading decision is yours to make.",
        "ar": "لا يمكننا تقديم نصائح أو توصيات أو توقعات تداول، لذلك لا نستطيع أن نحدد لك ماذا "
        "تتداول أو متى. قرار التداول يعود إليك وحدك.",
        "es": "No podemos ofrecer asesoramiento, recomendaciones ni predicciones de trading, por "
        "lo que no podemos indicarle qué operar ni cuándo. Cualquier decisión de trading es "
        "solo suya.",
        "fr": "Nous ne pouvons pas fournir de conseils, de recommandations ni de prévisions de "
        "trading ; nous ne pouvons donc pas vous dire quoi trader ni quand. Toute décision de "
        "trading vous appartient.",
        "pt": "Não podemos oferecer aconselhamento, recomendações ou previsões de trading, por "
        "isso não podemos dizer o que negociar nem quando. Qualquer decisão de trading cabe "
        "apenas a você.",
        "ru": "Мы не можем давать торговые советы, рекомендации или прогнозы, поэтому не можем "
        "подсказать, чем и когда торговать. Любое торговое решение принимаете только вы.",
        "id": "Kami tidak dapat memberikan saran, rekomendasi, atau prediksi trading, sehingga "
        "kami tidak dapat memberi tahu Anda apa yang harus ditradingkan atau kapan. Setiap "
        "keputusan trading sepenuhnya ada di tangan Anda.",
        "ms": "Kami tidak dapat memberikan nasihat, cadangan atau ramalan dagangan, jadi kami "
        "tidak boleh memberitahu anda apa yang perlu didagangkan atau bila. Sebarang keputusan "
        "dagangan adalah hak anda sepenuhnya.",
        "th": "เราไม่สามารถให้คำแนะนำ ข้อเสนอแนะ หรือการคาดการณ์เกี่ยวกับการเทรดได้ "
        "จึงไม่สามารถบอกได้ว่าคุณควรเทรดอะไรหรือเมื่อใด การตัดสินใจเทรดทั้งหมดเป็นของคุณเอง",
        "vi": "Chúng tôi không thể đưa ra lời khuyên, khuyến nghị hay dự đoán giao dịch, vì vậy "
        "chúng tôi không thể cho bạn biết nên giao dịch gì hoặc khi nào. Mọi quyết định giao "
        "dịch là của riêng bạn.",
        "zh-TW": "我們無法提供交易建議、推薦或預測，因此無法告訴您應該交易什麼或何時交易。"
        "任何交易決定均由您自行作出。",
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


def resolve(key: str, language: str) -> str | None:
    """The language key that has a text for this template (exact, alias, base), or None."""
    texts = TEMPLATES[key]
    for cand in (language, ALIASES.get(language), language.split("-")[0]):
        if cand and cand in texts:
            return cand
    return None


def render(key: str, language: str) -> tuple[str, bool]:
    """(text, in the requested language?). English when the language has no text."""
    lang = resolve(key, language)
    return (TEMPLATES[key][lang], True) if lang else (TEMPLATES[key]["en"], False)


def review_status(key: str, language: str) -> str:
    return REVIEW.get(resolve(key, language) or "en", "unknown")
