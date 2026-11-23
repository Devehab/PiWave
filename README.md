# PiWave — PiRadio

راديو FM للسيارة من Raspberry Pi Zero 2 W: الريسبيري بتعمل شبكة واي فاي خاصة فيها،
ومن صفحة على التلفون بتغيّر التردد، بترفع أغاني وبتشغلها، بتسجّل صوت وبتبثه،
وبتشوف إنه البث شغّال. كمان بيشتغل **سماعة بلوتوث** (أي صوت من التلفون بيطلع على الراديو)،
وفيه زر **احكي مباشر** زي اللاسلكي. التحديثات بتنرفع من نفس الصفحة بدون ما تعيد تنزيل النظام.

FM car radio station for a Raspberry Pi Zero 2 W (transmits on GPIO4 / physical pin 7).
The Pi runs its own Wi-Fi hotspot and a web page for frequency, songs, voice clips,
signal check and in-place updates. Since 1.1.0 it is also a Bluetooth speaker (A2DP sink:
whatever the phone plays goes out on FM), has push-to-talk from the phone's microphone,
and a welcome page that pops up only once per phone.

| Folder | What it is |
|---|---|
| `piradio/` | The product. Copy this folder to the SD card (`bootfs`) or clone the repo on the Pi, then run `sudo bash piradio/install.sh` |
| `dev/` | Tests that run on a normal computer: a PiFmRds simulator (`pi_fm_rds_sim.c`), a fake BlueZ (`fake_busctl.py`), the end-to-end suite (`test_all.py`) and `make-update.sh` (builds `dist/piradio-update-<version>.tar.gz`) |
| `دليل-التركيب.html` | Full Arabic install and usage guide |

Version: see `piradio/VERSION`. Each release has its own branch (`release/<version>`) and tag (`v<version>`).

Transmitter: [PiFmRds](https://github.com/ChristopheJacquet/PiFmRds) (GPLv3), vendored in `piradio/pifmrds/` with a raw-audio input patch.

> Transmitting on FM without a licence may be illegal where you live. Keep the power and the antenna small (20 cm wire) so it only reaches your own car.
