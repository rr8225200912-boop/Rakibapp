# APK Builder Bot

Telegram-এ Android প্রজেক্টের zip পাঠালে APK ফেরত দেয়। বিল্ডের আগে অ্যাপের **নাম** ও **লোগো (PNG)** চায়।

## সেটআপ (সবটাই ফোন থেকে)

1. **BotFather**: Telegram-এ @BotFather → `/newbot` → নাম দাও → **টোকেন** কপি করো।
2. **GitHub**: নতুন repository খোলো → Add file → Upload files → `bot.py`, `Dockerfile`, `README.md` আপলোড করে Commit করো।
3. **Railway**: New Project → Deploy from GitHub repo → ওই repository বেছে নাও।
4. Railway → service → **Variables**: `BOT_TOKEN` = BotFather-এর টোকেন (`OWNER_ID` এখন দিও না)।
5. Deploy শেষ হলে বটকে `/start` পাঠাও। সে তোমার **ID** বলবে।
6. Variables-এ `OWNER_ID` = ওই ID দাও। বট রিস্টার্ট হবে, তখন থেকে শুধু তুমি ব্যবহার করতে পারবে।

## ব্যবহার

1. প্রজেক্টের zip পাঠাও (২০ MB-র কম, `build` ও `.gradle` ফোল্ডার ছাড়া)।
2. বট নাম চাইবে → লেখো (বা `/skip`)।
3. বট লোগো চাইবে → PNG পাঠাও (বা `/skip`)। File/Document হিসেবে পাঠালে কোয়ালিটি ভালো থাকে।
4. বিল্ড শেষে APK আসবে। ফেল করলে কারণ ও লগ ফাইল আসবে, লগটা Claude-কে পাঠালে ঠিক করে দেবে।

কমান্ড: `/skip` `/status` `/cancel` `/help`

## জানা দরকার

- Gradle বিল্ডে অনেক RAM লাগে। কমপক্ষে ৪ GB RAM-এর প্ল্যান ভালো। কম হলে বট নিজে মেমোরি কমিয়ে চেষ্টা করে, তবু মাঝপথে থামতে পারে।
- প্রথম বিল্ড ধীর (১০–২০ মিনিট), কারণ লাইব্রেরি নামায়।
- APK debug-signed: ইনস্টল হয়, Play Store-এর জন্য নয়।
- `OWNER_ID` সেট না করলে বট কাউকে বিল্ড করতে দেয় না।

## ঐচ্ছিক Variables

`GRADLE_XMX` (যেমন `2g`), `BUILD_TIMEOUT_MIN` (ডিফল্ট `40`), `GRADLE_TASK` (ডিফল্ট `assembleDebug`)

ঐচ্ছিক: Railway-তে একটা Volume `/data` পথে লাগালে Gradle ক্যাশ সেখানে জমে, পরের বিল্ড দ্রুত হয়।
