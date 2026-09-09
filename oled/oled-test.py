from luma.core.interface.serial import i2c
from luma.oled.device import ssd1306
from luma.core.render import canvas
from time import sleep

serial = i2c(port=1, address=0x3C)
device = ssd1306(serial, width=128, height=64)

with canvas(device) as draw:
    draw.text((0, 0), "PiWatch Online", fill="white")
    draw.text((0, 16), "OLED works :)", fill="white")

sleep(10)
