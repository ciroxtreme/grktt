import os
import sys
import time
import json
import urllib.request
import urllib.parse
import urllib.error
from playwright.sync_api import sync_playwright
try:
    from playwright_stealth import Stealth
    HAS_STEALTH = True
except Exception:
    HAS_STEALTH = False

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SESSION_FILE = "session_grok.json"

def _send_one_chunk(url, payload, chunk_no, total):
    """Kirim 1 chunk; kalau Telegram balas 429, tunggu retry_after-nya lalu coba lagi (max 3x)."""
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/x-www-form-urlencoded"})
            urllib.request.urlopen(req, timeout=10)
            return True
        except urllib.error.HTTPError as e:
            wait = 2.0
            try:
                body = json.loads(e.read().decode("utf-8", "ignore"))
                wait = float(body.get("parameters", {}).get("retry_after", 2))
            except Exception:
                pass
            print(f"[TG] chunk {chunk_no}/{total}: 429 (coba {attempt}/3), tunggu {wait}s lalu retry")
            time.sleep(wait)
        except Exception as e:
            print(f"[TG Error] chunk {chunk_no}/{total}: {e}")
            return False
    print(f"[TG Error] chunk {chunk_no}/{total}: gagal setelah 3x retry (429)")
    return False

def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] No token/chat_id configured.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    chunk_size = 4000
    chunks = [text[i:i+chunk_size] for i in range(0, len(text), chunk_size)]
    for i, chunk in enumerate(chunks):
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk
        }).encode("utf-8")
        _send_one_chunk(url, data, i + 1, len(chunks))
        if len(chunks) > 1 and i < len(chunks) - 1:
            time.sleep(2.0)
    if len(chunks) > 1:
        print(f"[TG] Jawaban panjang dipecah jadi {len(chunks)} pesan (total {len(text)} karakter)")

def send_chat_screenshot(photo_path: str, caption: str = ""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if not os.path.exists(photo_path):
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        body = []
        body.append(f"--{boundary}".encode())
        body.append(f'Content-Disposition: form-data; name="chat_id"'.encode())
        body.append(''.encode())
        body.append(str(TELEGRAM_CHAT_ID).encode())
        
        if caption:
            body.append(f"--{boundary}".encode())
            body.append(f'Content-Disposition: form-data; name="caption"'.encode())
            body.append(''.encode())
            body.append(caption[:1024].encode())

        filename = os.path.basename(photo_path)
        body.append(f"--{boundary}".encode())
        body.append(f'Content-Disposition: form-data; name="photo"; filename="{filename}"'.encode())
        body.append('Content-Type: image/png'.encode())
        body.append(''.encode())
        with open(photo_path, "rb") as f:
            body.append(f.read())
        body.append(f"--{boundary}--".encode())
        body.append(''.encode())

        data = b"\r\n".join(body)
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(data))
        }
        req = urllib.request.Request(url, data=data, headers=headers)
        urllib.request.urlopen(req, timeout=15)
        print(f"[TG Photo Sent] {photo_path}")
    except Exception as e:
        print(f"[TG Photo Error] {e}")

def find_active_composer(page):
    """Cari input composer yang benar-benar siap diketik di UI Grok."""
    # Berdasarkan screenshot UI Grok: ada placeholder '+ Ask, create images, or build anything...'
    selectors = [
        "textarea[placeholder*='Ask' i]",
        "textarea[placeholder*='create images' i]",
        "textarea[placeholder*='Grok' i]",
        "textarea",
        "div[contenteditable='true']",
        "[role='textbox']",
        "input[placeholder*='Ask' i]"
    ]
    for s in selectors:
        try:
            loc = page.locator(s)
            if loc.count() > 0:
                for i in range(loc.count()):
                    item = loc.nth(i)
                    if item.is_visible(timeout=1000):
                        return item
        except Exception:
            continue
    return None

def check_login_page(page) -> bool:
    """Deteksi apakah terlempar ke halaman Login / Sign In."""
    content = page.content().lower()
    current_url = page.url.lower()
    if ("sign in" in content and "create an account" in content and not "chat baru" in content):
        return True
    if "/login" in current_url or "/signin" in current_url:
        return True
    return False

JS_CHECK_GROK_STREAM = """
() => {
  const bubbles = document.querySelectorAll(".message-bubble, [data-message-author-role='assistant'], div[class*='response'], div[class*='message'], article");
  let len = 0;
  if (bubbles.length > 0) {
    const last = bubbles[bubbles.length - 1];
    len = (last.innerText || "").length;
  }
  let gen = false;
  document.querySelectorAll("button").forEach(b => {
    const label = (b.getAttribute("aria-label") || "") + " " + (b.textContent || "");
    if (/stop/i.test(label) && b.offsetParent !== null) gen = true;
  });
  return { answerLen: len, isGenerating: gen };
}
"""

def wait_for_grok_answer_done(page, timeout_sec=900, idle_sec=300):
    """Tunggu jawaban Grok selesai dengan mengikuti jawaban (bukan cap tetap).
    Selesai kalau: panjang jawaban stabil 8 detik + tidak generating,
    atau stabil 60 detik walau indikator generating nyangkut.
    Idle 300 detik tanpa pertumbuhan -> 'idle'. Cap absolut -> 'cap'."""
    last_len = -1
    stable_ticks = 0
    last_growth = time.time()
    start = time.time()
    while True:
        elapsed = time.time() - start
        try:
            st = page.evaluate(JS_CHECK_GROK_STREAM)
        except Exception as e:
            print(f"[wait] evaluate gagal: {e}")
            st = {"answerLen": last_len if last_len >= 0 else 0, "isGenerating": True}
        cur_len = st.get("answerLen", 0)
        gen = st.get("isGenerating", False)
        if cur_len != last_len:
            last_len = cur_len
            stable_ticks = 0
            last_growth = time.time()
        else:
            stable_ticks += 1
        if int(elapsed) % 30 < 2 and int(elapsed) > 0:
            print(f"[{int(elapsed)}s] len={cur_len} gen={gen}")
        if cur_len > 0 and stable_ticks >= 4 and not gen:
            print(f"[wait] Selesai: jawaban stabil {stable_ticks * 2}s, tidak generating ({cur_len} karakter).")
            return "done"
        if cur_len > 0 and stable_ticks >= 30:
            print(f"[wait] Selesai (slow path): panjang stabil {stable_ticks * 2}s ({cur_len} karakter).")
            return "done"
        if time.time() - last_growth > idle_sec and not gen:
            print(f"[wait] Idle {idle_sec}s tanpa pertumbuhan.")
            return "idle"
        if elapsed >= timeout_sec:
            print(f"[{int(elapsed)}s] Pengaman absolut {timeout_sec}s tercapai.")
            return "cap"
        time.sleep(2)

def main():
    prompt = "Halo Grok, perkenalkan dirimu secara singkat."
    if len(sys.argv) > 1 and sys.argv[1].strip():
        prompt = sys.argv[1].strip()

    print(f"👉 Menyiapkan pertanyaan ke Grok: '{prompt}'...")

    proxy_cfg = {"server": "socks5://127.0.0.1:40000"}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy=proxy_cfg,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--window-size=1280,800"
            ]
        )

        context_kwargs = {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "locale": "id-ID",
            "timezone_id": "Asia/Jakarta"
        }

        if os.path.exists(SESSION_FILE):
            try:
                context_kwargs["storage_state"] = SESSION_FILE
                print("✓ Memuat sesi persisten dari session_grok.json")
            except Exception as e:
                print(f"[Warn] Gagal memuat session_grok.json: {e}")

        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        if HAS_STEALTH:
            try:
                stealth = Stealth()
                stealth.apply_stealth_sync(page)
                print("✓ Stealth applied.")
            except Exception as e:
                print(f"[Warn] Stealth error: {e}")

        print("🌐 Mengakses https://grok.com/ ...")
        try:
            page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)
        except Exception as e:
            print(f"[Nav Error] {e}")

        # Tunggu hingga halaman siap dan cari composer
        composer = None
        for attempt in range(6):
            composer = find_active_composer(page)
            if composer:
                print(f"✓ Composer ditemukan pada detik ke-{attempt * 3}!")
                break
            time.sleep(3)

        # Cek apakah terlempar ke layar Sign In murni
        if not composer and check_login_page(page):
            print("❌ Sesi expired: Terlempar ke halaman Sign In.")
            page.screenshot(path="err_need_login.png")
            send_chat_screenshot("err_need_login.png", "❌ Grok: Sesi expired (terlempar ke Sign In).")
            browser.close()
            sys.exit(1)

        # Ambil screenshot tampilan dashboard terverifikasi
        page.screenshot(path="01_grok_view.png")

        if not composer:
            print("❌ Input textarea tidak ditemukan setelah ditunggu.")
            send_chat_screenshot("01_grok_view.png", "❌ Grok: Input chat box tidak ditemukan.")
            browser.close()
            sys.exit(1)

        # Simpan state cookies terbaru karena dashboard aktif terbukti valid
        try:
            context.storage_state(path=SESSION_FILE)
            print("✓ State cookie dashboard berhasil diperbarui.")
        except Exception as e:
            print(f"[Session Save Error] {e}")

        # Mode keep-alive (dipakai run cron tiap 6 jam): cukup verifikasi
        # dashboard + simpan state, tanpa nanya apa-apa (biar nggak spam jawaban).
        if os.environ.get("GROK_KEEPALIVE") == "true":
            print("[KEEP-ALIVE] Session Grok masih aktif, state tersimpan. Tidak ada prompt.")
            browser.close()
            return

        print("✓ Dashboard aktif terverifikasi! Mengetik pertanyaan...")
        try:
            composer.click(force=True)
            time.sleep(0.5)
            composer.fill(prompt)
        except Exception:
            page.keyboard.type(prompt, delay=15)

        time.sleep(1)
        # Cari tombol kirim atau tekan Enter
        send_btn = page.locator("button[aria-label*='Send' i], button[aria-label*='Submit' i], button:has-text('Send')")
        if send_btn.count() > 0 and send_btn.first.is_enabled():
            send_btn.first.click()
        else:
            page.keyboard.press("Enter")

        print("✓ Pertanyaan terkirim. Menunggu respons Grok streaming...")
        time.sleep(6)

        # Tunggu jawaban selesai mengikuti jawaban (bukan cap tetap 150 detik):
        # berhenti saat jawaban stabil + tidak generating, atau cap absolut 900 detik.
        wait_for_grok_answer_done(page, timeout_sec=900, idle_sec=300)

        time.sleep(3)
        page.screenshot(path="02_grok_answer.png")

        # Ekstraksi jawaban dari bubble response
        answer = ""
        bubbles = page.locator(".message-bubble, [data-message-author-role='assistant'], div[class*='response'], div[class*='message'], article")
        if bubbles.count() > 0:
            for i in range(bubbles.count() - 1, -1, -1):
                txt = bubbles.nth(i).inner_text().strip()
                if txt and prompt not in txt and len(txt) > 10:
                    answer = txt
                    break

        if not answer:
            ps = page.locator("main p, [role='main'] p")
            p_texts = [ps.nth(i).inner_text().strip() for i in range(ps.count()) if ps.nth(i).inner_text().strip()]
            if p_texts:
                answer = "\n\n".join(p_texts)

        if answer:
            print("\n================== JAWABAN DARI GROK ==================")
            print(answer)
            print("=======================================================\n")
            msg = f"⚡ *JAWABAN GROK*\n\n{answer}"
            send_telegram_message(msg)
            send_chat_screenshot("02_grok_answer.png", "Tangkapan layar respons Grok")
        else:
            print("⚠️ Gagal mengekstrak teks otomatis, mengirim bukti tangkapan layar...")
            send_chat_screenshot("02_grok_answer.png", "⚠️ Grok merespons (detail ada di tangkapan layar).")

        context.storage_state(path=SESSION_FILE)
        browser.close()

if __name__ == "__main__":
    main()
