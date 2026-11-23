"""English for the messages the radio sends to the web page.

The code raises its messages in Arabic (the original language of the
project); `tr(text, "en")` turns them into English just before they are sent,
so a message that is missing here still reaches the user (in Arabic) instead
of breaking anything.
"""
import re

EXACT = {
    # bluetooth
    "البلوتوث مش جاهز": "Bluetooth is not ready",
    "الجهاز مش موجود": "That device was not found",
    "في عملية شغالة، استنى ثواني": "Something is already in progress, wait a few seconds",
    "التلفون مش قريب أو البلوتوث عليه مطفي": "The phone is not nearby or its Bluetooth is off",
    "التلفون ما رد. تأكد إنه البلوتوث عليه شغال وقريب":
        "The phone didn't answer. Make sure its Bluetooth is on and it is nearby",
    "التلفون رفض الاتصال. افتح البلوتوث بالتلفون واختار PiRadio":
        "The phone refused. Open Bluetooth on the phone and choose PiRadio",
    "خدمة البلوتوث مش شغالة": "The Bluetooth service is not running",
    "خطأ بالبلوتوث": "Bluetooth error",
    "ما لقيت قطعة البلوتوث بالجهاز": "No Bluetooth adapter found on the Pi",
    "البلوتوث مش مثبت على الجهاز": "Bluetooth is not installed on the Pi",
    "شغّل البلوتوث أول": "Turn Bluetooth on first",
    "أمر مش معروف": "Unknown command",
    "التلفون ما بيسمح بالتحكم من هون. تحكم من التلفون نفسه":
        "This phone doesn't allow remote control. Use the phone itself",
    "التثبيت شغال هلأ": "The installation is already running",
    "سكربت التثبيت مش موجود": "The setup script is missing",
    "ما في إنترنت. وصّل الجهاز بواي فاي البيت (الإعدادات ← اتصل بواي فاي البيت) وجرّب مرة ثانية.":
        "No internet. Connect the Pi to your home Wi-Fi (Settings → Join home Wi-Fi) and try again.",
    # transmitter / player
    "برنامج البث محتاج صلاحيات root (/dev/mem)": "The transmitter needs root rights (/dev/mem)",
    "مشكلة بالاتصال مع معالج الريسبيري (mailbox)":
        "Could not talk to the Raspberry Pi's processor (mailbox)",
    "ما في ذاكرة كافية للبث": "Not enough memory to transmit",
    "بدون رسالة": "no message",
    "الأغنية مش موجودة": "Song not found",
    "ما في أغاني. ارفع أغاني أول.": "No songs yet. Upload some first.",
    "نغمة اختبار": "Test tone",
    "مايك مباشر": "Live mic",
    "في حدا عم يحكي على الهوا هلأ. استنى يخلص":
        "Someone is talking on air right now. Wait until they finish",
    "النسخة الجديدة ما قدرت تبث خلال 90 ثانية":
        "The new version could not go on air within 90 seconds",
    # Wi-Fi
    "اسم الشبكة لازم يكون من 1 إلى 32 حرف": "The network name must be 1 to 32 characters",
    "كلمة السر لازم تكون من 8 إلى 63 حرف إنجليزي/رقم":
        "The password must be 8 to 63 English letters/digits",
    # settings
    "التردد لازم يكون رقم، مثل 107.9": "The frequency must be a number, like 107.9",
    "التردد لازم يكون بين 76.0 و 108.0 ميغاهيرتز": "The frequency must be between 76.0 and 108.0 MHz",
    "اسم المحطة: من 1 إلى 8 أحرف إنجليزية/أرقام فقط":
        "Station name: 1 to 8 English letters/digits only",
    "النص لازم يكون 64 حرف أو أقل": "The text must be 64 characters or less",
    "رمز PI لازم يكون 4 خانات hex مثل E2A1": "The PI code must be 4 hex digits, like E2A1",
    "قيمة ppm غير صحيحة": "Invalid ppm value",
    "قيمة ppm لازم تكون بين -500 و 500": "ppm must be between -500 and 500",
    "قيمة الصوت غير صحيحة": "Invalid volume",
    "قيمة pre-emphasis غير صحيحة": "Invalid pre-emphasis value",
    "وضع التكرار غير صحيح": "Invalid repeat mode",
    "اسم البلوتوث: من 1 إلى 30 حرف": "Bluetooth name: 1 to 30 characters",
    "قيمة صفحة الترحيب غير صحيحة": "Invalid welcome-page setting",
    "اللغة لازم تكون en أو ar": "Language must be en or ar",
    # library / uploads
    "هذا الملف مش ملف صوت يمكن تشغيله": "This file is not an audio file that can be played",
    "انتهى البث المباشر": "The live broadcast has ended",
    "الملف كبير كثير (الحد 1GB)": "The file is too big (limit 1 GB)",
    "ما قدرت أشغّل": "Could not play",
    "ما في أغاني للتشغيل": "No songs to play",
    "ما وصل ملف": "No file received",
    "الملف فاضي": "The file is empty",
    "قطعة صوت كبيرة كثير": "Audio chunk too large",
    # updates
    "في تحديث شغال هلأ، استنى يخلص": "An update is already running, wait for it to finish",
    "رجوع يدوي من الصفحة": "Manual rollback from the page",
    "ما في إصدار سابق للرجوع إله": "There is no previous version to go back to",
    "ملف التحديث فيه ملفات كثيرة كثير": "The update file contains too many files",
    "ملف التحديث كبير كثير": "The update file is too big",
    "هذا مش ملف تحديث (لازم يكون .tar.gz أو .zip)": "This is not an update file (it must be .tar.gz or .zip)",
    "ملف التحديث ناقص: ما لقيت VERSION و app/main.py":
        "The update file is incomplete: VERSION and app/main.py are missing",
    "ملف التحديث ما فيه MANIFEST.sha256 (مش ملف رسمي)":
        "The update file has no MANIFEST.sha256 (not an official package)",
    "بناء برنامج البث فشل (التفاصيل بالسجل)": "Building the transmitter failed (details in the log)",
    "رقم الإصدار (VERSION) مش صحيح": "The version number (VERSION) is invalid",
    "سكربت ما بعد التحديث فشل": "The post-update script failed",
}

PATTERNS = [
    (r"^ما قدرت أجهز قناة التحكم: (.*)$", "Could not set up the RDS control channel: {0}"),
    (r"^ما قدرت أشغّل برنامج البث \((.*?)\): (.*)$", "Could not start the transmitter ({0}): {1}"),
    (r"^برنامج البث وقف \(رمز (.*?)\): (.*)$", "The transmitter stopped (code {0}): {1}"),
    (r"^ما قدرت أشغّل ffmpeg للبلوتوث: (.*)$", "Could not start ffmpeg for Bluetooth: {0}"),
    (r"^ما قدرت أشغّل ffmpeg: (.*)$", "Could not start ffmpeg: {0}"),
    (r"^تعذّر تشغيل «(.*?)»: (.*)$", "Could not play “{0}”: {1}"),
    (r"^تعذّر تشغيل «(.*?)»$", "Could not play “{0}”"),
    (r"^ما قدرت أتصل بـ (.*?): (.*)$", "Could not connect to {0}: {1}"),
    (r"^ما قدرت أغيّر الشبكة: (.*)$", "Could not change the network: {0}"),
    (r"^إعداد غير معروف: (.*)$", "Unknown setting: {0}"),
    (r"^ملف التحديث فيه مسار غير مسموح: (.*)$", "The update file contains a forbidden path: {0}"),
    (r"^ملفات ناقصة أو خربانة: (.*)$", "Missing or damaged files: {0}"),
    (r"^كود النسخة الجديدة فيه خطأ: (.*)$", "The new version's code has an error: {0}"),
    (r"^النسخة الجديدة وقفت (\d+) مرات عند التشغيل$", "The new version crashed {0} times at start-up"),
]
_COMPILED = [(re.compile(p, re.S), t) for p, t in PATTERNS]


def tr(text, lang):
    """Arabic message -> the user's language (only English needs work)."""
    if lang != "en" or not isinstance(text, str) or not text:
        return text
    if text in EXACT:
        return EXACT[text]
    for rx, tmpl in _COMPILED:
        m = rx.match(text)
        if m:
            return tmpl.format(*(tr(g or "", lang) for g in m.groups()))
    return text
