#!/usr/bin/env python3
"""
Dò lỗi còi trên GPIO5 (pin 29): còi chủ động hay thụ động, có nhận tín hiệu không.

Chạy ở terminal cạnh mạch, khi aiot-controller đã dừng (bash scripts/pi_up.sh --stop):
    cd ~/BTL_IOT && venv/bin/python scripts/buzzer_probe.py

- Bước 1: giữ I/O ở LOW 3 giây. Còi CHỦ ĐỘNG kích mức thấp phải kêu liên tục.
- Bước 2, 3: phát xung 2 kHz rồi 1 kHz trong 3 giây. Còi THỤ ĐỘNG chỉ kêu ở bước này
  (với mức DC nó chỉ "tách" một tiếng nhỏ).
Kết thúc luôn để chân ở HIGH (mức im của module kích mức thấp) rồi nhả chân.
"""
import sys
import time

import lgpio

PIN = 5
CHIP = 0


def ask(question):
    while True:
        a = input(f"  {question} (c/k): ").strip().lower()
        if a in ("c", "k"):
            return a == "c"


def main():
    if not sys.stdin.isatty():
        print("Cần chạy ở terminal cạnh mạch (ssh -t ...)")
        return 1
    h = lgpio.gpiochip_open(CHIP)
    results = {}
    try:
        lgpio.gpio_claim_output(h, PIN, 1)
        input("Áp tai gần còi rồi nhấn Enter...")

        print("Bước 1: I/O = LOW trong 3 giây")
        lgpio.gpio_write(h, PIN, 0)
        time.sleep(3)
        lgpio.gpio_write(h, PIN, 1)
        results["low"] = ask("Còi có kêu liên tục không?")

        for freq in (2000, 1000):
            print(f"Bước: phát xung {freq} Hz trong 3 giây")
            lgpio.tx_pwm(h, PIN, freq, 50)
            time.sleep(3)
            lgpio.tx_pwm(h, PIN, 0, 0)
            time.sleep(0.2)          # luồng PWM của lgpio dừng không đồng bộ
            lgpio.gpio_write(h, PIN, 1)
            results[freq] = ask("Còi có kêu (tiếng bíp cao) không?")
    finally:
        try:
            lgpio.tx_pwm(h, PIN, 0, 0)
            time.sleep(0.2)          # luồng PWM của lgpio dừng không đồng bộ
            lgpio.gpio_write(h, PIN, 1)
        finally:
            lgpio.gpiochip_close(h)

    print("\n===== KẾT LUẬN =====")
    if results["low"]:
        print("Còi CHỦ ĐỘNG, nhận tín hiệu tốt. Nếu test_all vẫn báo lỗi thì do trả lời nhầm; chạy lại test_all.")
    elif results[2000] or results[1000]:
        print("Còi THỤ ĐỘNG: cần phát xung (PWM) mới kêu. Báo lại để chuyển code còi sang phát xung.")
    else:
        print("Không kêu ở cả 3 bước. Kiểm tra theo thứ tự:")
        print("  1. Dây: VCC pin 17 (3.3V), GND pin 25, I/O pin 29; cắm lại chắc đầu Dupont.")
        print("  2. Tắt Pi, chuyển VCC sang pin 4 (5V) rồi chạy lại script này:")
        print("     - kêu ở bước 1 nhưng không tắt được: còi cần 5V, 3.3V quá yếu -> báo lại để đổi cách nối")
        print("     - vẫn im: module hoặc còi hỏng")
    return 0


if __name__ == "__main__":
    sys.exit(main())
