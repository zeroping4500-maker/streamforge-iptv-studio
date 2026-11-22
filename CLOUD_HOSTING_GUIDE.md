# ☁️ 24/7 Always-On Cloud Hosting Guide for StreamForge IPTV Studio

This guide explains how to host **StreamForge IPTV Studio** online for **free** so you can access your IPTV studio panel and M3U playlists from your phone, TV, or browser **24/7, even when your PC is turned off**.

---

## 🏆 Option 1: Render.com (Recommended - 100% Free & Automatic HTTPS)

**Why Render:** Render gives you a free web service with free automated SSL (`https://your-studio.onrender.com`), requires zero credit card, and auto-deploys via GitHub or direct ZIP upload.

### Step-by-Step (Under 3 Minutes):
1. **Download Deployment Bundle:**
   - Inside StreamForge IPTV Studio, click **☁️ 24/7 Cloud Host** in the top navbar and click **Download Cloud Bundle ZIP** (or use the files in this directory).
2. **Push to GitHub:**
   - Create a free GitHub repository (e.g. `streamforge-iptv-studio`) at [github.com/new](https://github.com/new).
   - Upload the files or push the repository.
3. **Deploy on Render:**
   - Sign up for free at [render.com](https://render.com).
   - Click **New +** -> **Web Service**.
   - Select your `streamforge-iptv-studio` repository.
   - Render will automatically read `render.yaml` / `Dockerfile`:
     - **Runtime:** Python or Docker
     - **Build Command:** `pip install -r requirements.txt`
     - **Start Command:** `python app.py`
     - **Instance Type:** Free
   - Click **Deploy Web Service**!
4. **Access Anytime:**
   - In ~90 seconds, you get your permanent live URL: `https://streamforge-iptv-studio.onrender.com`.

---

## 🚀 Option 2: Koyeb (Always-On Global Free Tier)

**Why Koyeb:** Koyeb provides free nano instances with continuous uptime and high-speed global CDN routing.

1. Create a free account at [koyeb.com](https://www.koyeb.com).
2. Click **Create App** -> **GitHub**.
3. Select your repository. Koyeb will auto-detect the `Dockerfile`.
4. Click **Deploy**. Your app will be live at `https://<your-app>.koyeb.app`!

---

## 🤗 Option 3: Hugging Face Spaces (Instant Free Docker Container)

**Why Hugging Face:** 100% free 2-CPU 16GB RAM cloud container, zero credit card, always available.

1. Create a free account at [huggingface.co](https://huggingface.co).
2. Click **New Space** -> select **Docker** as Space SDK.
3. Choose **Blank** and name it `streamforge-iptv-studio`.
4. Upload `app.py`, `Dockerfile`, `requirements.txt`, and the `static/` folder (or push via git).
5. Hugging Face builds the Docker container automatically and provides a live HTTPS link!

---

## 🖥️ Option 4: Free Cloud VPS (Oracle Cloud Always-Free or DigitalOcean)

If you have a Linux VPS:
```bash
git clone <your-repo>
cd iptv-studio
docker compose up -d
```
The studio runs in the background 24/7 with restart protection on port 8765!
