PiRadio 1.2.0 — راديو السيارة (Raspberry Pi Zero 2 W)
==================================================

1) انسخ هذا المجلد "piradio" كما هو إلى قرص bootfs في كرت الذاكرة.
   (اختياري) عدّل piradio.conf: اسم الواي فاي، كلمة السر، التردد، اسم المحطة.

2) شغّل الريسبيري على واي فاي البيت، ومن Terminal على الماك:
       ssh pi@piradio.local
       sudo bash /boot/firmware/piradio/install.sh
   إذا طلع "No such file": ابعت المجلد من الماك:  scp -r piradio pi@piradio.local:~/
   وبعدين:  sudo bash ~/piradio/install.sh

3) ركّب سلك 20 سم على الرجل رقم 7 (GPIO4) فقط.

4) بالسيارة: اتصل بشبكة "PiRadio" (كلمة السر piradio123)
   صفحة الترحيب بتطلع مرة وحدة: اضغط "تم". لوحة التحكم: http://192.168.4.1
   وراديو السيارة على FM نفس التردد.

5) البلوتوث: من الصفحة > بلوتوث > وصّل تلفون جديد، وبالتلفون اختار "PiRadio".
   احكي مباشر: من الصفحة > احكي مباشر (اضغط مطوّل واحكي).

التحديث: من الصفحة > الإعدادات > التحديث > ارفع ملف piradio-update-X.Y.Z.tar.gz

----
1) Copy this "piradio" folder to the SD card's "bootfs" drive.
2) On the Pi (home Wi-Fi):  sudo bash /boot/firmware/piradio/install.sh
3) 20 cm wire on physical pin 7 (GPIO4) only.
4) Join Wi-Fi "PiRadio" (password piradio123), tap "Done" on the welcome page,
   control page: http://192.168.4.1
5) Bluetooth speaker "PiRadio": pair from the page (Bluetooth > pairing mode).
   Push-to-talk: page > "احكي مباشر".
