"""Generate assets/banner.png for bitpirate-mcp (1600x500, dark theme, crisp text)."""
import random
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1600, 500
BG = (10, 14, 22)
TEAL = (45, 212, 191)
TEAL_DIM = (38, 170, 155)
WHITE = (240, 246, 252)
GREY = (148, 163, 178)
GREY_DIM = (110, 125, 145)
PCB = (13, 52, 40)
PCB_EDGE = (30, 92, 72)
PCB_TRACE = (36, 92, 70)
SILVER = (150, 158, 170)
SILVER_HI = (196, 191, 204)
CHIP = (35, 40, 48)
CHIP_EDGE = (60, 68, 80)
GOLD = (212, 180, 90)

B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
M = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

f_title = ImageFont.truetype(B, 84)
f_sub = ImageFont.truetype(R, 27)
f_badge = ImageFont.truetype(B, 22)
f_silk = ImageFont.truetype(M, 14)
f_chip = ImageFont.truetype(M, 17)
f_conn = ImageFont.truetype(R, 18)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# ---- background circuit traces (deterministic) ----
random.seed(42)
for i in range(26):
    x, y = random.randint(0, W), random.randint(0, H)
    col = (28, 42, 60)
    pts = [(x, y)]
    for _ in range(3):
        if random.random() < 0.5:
            x += random.choice([-1, 1]) * random.randint(80, 300)
        else:
            y += random.choice([-1, 1]) * random.randint(50, 240)
        pts.append((x, y))
    d.line(pts, fill=col, width=2)
    d.ellipse([x - 4, y - 4, x + 4, y + 4], outline=col, width=2)
for i in range(10):
    x, y = random.randint(40, W - 40), random.randint(40, H - 40)
    d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=(52, 74, 100))

# ---- glow behind board ----
glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse([960, 30, 1560, 470], fill=(20, 120, 105, 90))
glow = glow.filter(ImageFilter.GaussianBlur(70))
img = Image.alpha_composite(img.convert("RGBA"), glow)
d = ImageDraw.Draw(img)

# ---- XIAO ESP32S3 board (stylized) ----
bx0, by0, bx1, by1 = 1060, 130, 1500, 370
d.rounded_rectangle([bx0, by0, bx1, by1], radius=18, fill=PCB, outline=PCB_EDGE, width=2)

# gold edge pads (XIAO: 11 per long side)
for i in range(11):
    px = bx0 + 47 + i * 32
    d.rectangle([px, by0 + 6, px + 26, by0 + 20], fill=GOLD)
    d.rectangle([px, by1 - 20, px + 26, by1 - 6], fill=GOLD)

# USB-C port top-left of board
d.rounded_rectangle([bx0 + 14, by0 + 26, bx0 + 88, by0 + 56], radius=12,
                    fill=SILVER_HI, outline=(120, 126, 138), width=2)
d.rounded_rectangle([bx0 + 22, by0 + 34, bx0 + 80, by0 + 48], radius=7,
                    fill=(60, 64, 74))

# main chip
cx0, cy0, cx1, cy1 = 1230, 175, 1390, 335
d.rounded_rectangle([cx0, cy0, cx1, cy1], radius=10, fill=CHIP, outline=CHIP_EDGE, width=2)
for i in range(8):
    t = cy0 + 12 + i * 19
    d.rectangle([cx0 - 10, t, cx0, t + 11], fill=SILVER)
    d.rectangle([cx1, t, cx1 + 10, t + 11], fill=SILVER)
for i in range(8):
    t = cx0 + 12 + i * 19
    d.rectangle([t, cy0 - 10, t + 11, cy0], fill=SILVER)
    d.rectangle([t, cy1, t + 11, cy1 + 10], fill=SILVER)
d.ellipse([cx0 + 14, cy0 + 14, cx0 + 24, cy0 + 24], outline=(90, 100, 115), width=2)
label = "ESP32-S3"
tw = d.textlength(label, font=f_chip)
d.text(((cx0 + cx1) / 2 - tw / 2, cy0 + 68), label, font=f_chip, fill=(205, 214, 223))

# crystal + passives
d.rounded_rectangle([1410, 195, 1472, 221], radius=8, fill=SILVER_HI, outline=(120, 126, 138), width=1)
for (rx, ry) in [(1410, 250), (1410, 275), (1410, 300)]:
    d.rectangle([rx, ry, rx + 34, ry + 14], fill=(120, 130, 145))
    d.rectangle([rx, ry, rx + 8, ry + 14], fill=(70, 78, 92))
    d.rectangle([rx + 26, ry, rx + 34, ry + 14], fill=(70, 78, 92))

# LEDs
d.ellipse([1170, 190, 1184, 204], fill=TEAL)
d.ellipse([1170, 216, 1184, 230], fill=(80, 95, 110))

# silkscreen (kept clear of the chip: chip left edge x=1230, top y=175)
d.text([bx0 + 14, by0 + 64], "BIT PIRATE", font=f_silk, fill=(190, 210, 198))
d.text([bx0 + 14, by1 - 44], "XIAO ESP32S3", font=f_silk, fill=(190, 210, 198))

# PCB traces from chip to pads (orthogonal)
for px, py in [(1100, 140), (1180, 140), (1420, 140), (1450, 350), (1180, 350)]:
    mx = (px + cx0) // 2
    d.line([(px, py), (mx, py), (mx, 255), (cx0, 255)], fill=PCB_TRACE, width=2)

# ---- dotted connector: text block -> board (label right-aligned clear of board) ----
d.line([(880, 250), (1054, 250)], fill=(70, 84, 105), width=2)
for x in range(880, 1054, 16):
    d.rectangle([x, 248, x + 8, 252], fill=(70, 84, 105))
ct = "USB CDC · 115200 baud"
ctw = d.textlength(ct, font=f_conn)
d.text((1050 - ctw, 214), ct, font=f_conn, fill=GREY_DIM)

# ---- title block ----
t1 = "Bit Pirate "
t2 = "MCP"
x = 70
d.text((x, 66), t1, font=f_title, fill=WHITE)
x += d.textlength(t1, font=f_title)
d.text((x, 66), t2, font=f_title, fill=TEAL)

d.text((70, 178), "Agent-driven multibus bench for the ESP32-Bit-Pirate", font=f_sub, fill=GREY)
d.text((70, 216), "UART · SPI · I²C · 1-Wire · DIO · PWM · on-device flash RE", font=f_sub, fill=GREY)

# ---- badges ----
badges = ["51 MCP tools", "On-device flash analysis", "92/92 protocol battery", "MIT"]
bx = 70
for b in badges:
    tw = d.textlength(b, font=f_badge)
    w = int(tw) + 36
    d.rounded_rectangle([bx, 300, bx + w, 342], radius=21, fill=(14, 40, 42),
                        outline=TEAL_DIM, width=2)
    d.text((bx + 18, 308), b, font=f_badge, fill=(170, 235, 220))
    bx += w + 18

d.text((70, 448), "github.com/Indigaz/bitpirate-mcp", font=f_conn, fill=(95, 112, 135))

img.convert("RGB").save("/home/christian/opt/bitpirate-mcp/assets/banner.png")
print("saved", img.size)
