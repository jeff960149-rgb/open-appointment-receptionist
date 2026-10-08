# 通用 AI 預約接待系統

這個專案從 Kspa 的 Kiki 與 Footprint 的 Karin 整理而來，希望讓**需要預約的店家都能少走一些冤枉路**。美容、美髮、按摩、寵物美容、維修服務等，只要能用「服務時長＋可用容量」安排預約，就可以從範例開始修改。

程式碼採 MIT 授權，可免費使用、修改與商用。電話、AI API、主機費用由使用者自己的服務商收取。

## 先免費試跑

需要 Python 3.11 以上。先下載專案，再執行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn receptionist.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Windows PowerShell 啟用環境用 `.venv\Scripts\Activate.ps1`；複製設定用 `Copy-Item .env.example .env`。

開啟 http://127.0.0.1:8000，選平日營業時間做測試預約。不用金鑰、不會通知店員，也不會寫入正式預約。按「確認」才會建立這次試用的預約，再查同一時間可以看到容量變化。

## 換成你的店

在 `.env` 指定 `BUSINESS_CONFIG=examples/salon.json`，重新啟動，就會使用美髮範例。也有寵物美容、維修、Kspa 與 Footprint 範例。

JSON 裡可以修改店名、助理名稱、時區、幣別、服務、各時長價格、營業時間、服務後整理時間與同時可接待的容量。範例的地址、班表與店員數是示意資料，使用前請換成自己的設定。

## 接電話與 Telegram

設定你自己的 OpenAI、Twilio 和 Telegram 帳號，依 [部署說明](deployment.md) 操作。AI 預設關閉，需要自行開啟。

- 客人可以先讓真人電話與 SIP 一起響鈴，無人接聽才交給 AI。
- 已確認的預約會存進本機資料庫，再通知店員。
- 客人要求真人時，先保存回電需求，不會叫他重打同一個號碼。
- Telegram 支援 `今天3人上班`、`明天有2個員工`、`/capacity 2030-01-07 3`，會儲存指定日期的容量。
- `/ai on`、`/ai off` 控制之後的 AI 轉接。

網頁的 AI 文字測試需要自己設定 API 金鑰與管理密鑰；會產生 API 用量，但只操作獨立測試資料。這不是完全離線的 AI。

## 目前的範圍

v0.1 是可自行架設的早期版本，每次部署服務一間店。共用核心以「可互換的人力／空間容量」判斷空位，尚未支援指定員工技能、付款、自動改期或 Google Calendar 同步。改期與客人取消需求先轉成店員回電；管理員可取消預約。

正式預約寫進這個服務的 SQLite 資料庫。Kspa 的 Smiley 整合與 Footprint 原程式保留在 `reference/`，已移除私人資料；**它們不會自動和共用核心同步**。

正式開店使用前，請用自己的電話測通整個流程，包括來電、問價格、空位不足、確認預約與店員收到通知。自動測試通過不代表每家店的電話設定都已驗證。

歡迎提交修正、新行業設定與預約系統整合，讓更多店家能用上。
