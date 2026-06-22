const fs = require("fs");

const BOT_TOKEN = "5719431335:AAHRk1vFhBqSQJbx7k1LmQWoFpR1SMf0QS8";

const chatIds = require("./chat_ids.json");
//[399163123];

const message = `سرور 🇨🇦 کانادا 🇨🇦 اضافه شد.

خرید از اینجا 👇
🤖👉https://t.me/hero_vpnbot?start

پشتیبانی:
📩@hero_support1`;

const report = {
  success: [],
  failed: [],
  retry: [],
};

let globalPause = false;
let resumeTime = 0;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function sendMessage(chatId) {
  try {
    const res = await fetch("https://tbot.omidmokhtari93.workers.dev", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        botToken: BOT_TOKEN,
        method: "sendMessage",
        payload: {
          chat_id: chatId,
          text: message,
        },
      }),
    });

    const data = await res.json();

    // 🚨 Rate limit
    if (!data.ok && data.parameters?.retry_after) {
      const waitTime = data.parameters.retry_after * 1000;

      console.log(`⛔ Rate limit → wait ${waitTime}ms`);

      globalPause = true;
      resumeTime = Date.now() + waitTime;

      await sleep(waitTime);

      globalPause = false;

      // دوباره باید retry بشه
      return { status: "retry" };
    }

    if (!data.ok) {
      console.log(`✗ Failed ${chatId}`, data.description);
      return { status: "failed", reason: data.description };
    }

    console.log(`✓ Sent ${chatId}`);
    return { status: "success" };
  } catch (err) {
    console.log(`✗ Error ${chatId}`, err.message);
    return { status: "failed", reason: err.message };
  }
}

async function worker(queue) {
  while (queue.length) {
    if (globalPause) {
      const wait = resumeTime - Date.now();
      if (wait > 0) await sleep(wait);
    }

    const chatId = queue.shift();
    if (!chatId) break;

    const result = await sendMessage(chatId);

    if (result.status === "success") {
      report.success.push(chatId);
    } else if (result.status === "failed") {
      report.failed.push({
        chatId,
        reason: result.reason,
      });
    } else if (result.status === "retry") {
      report.retry.push(chatId);
      queue.push(chatId); // برگرد به صف
    }

    await sleep(30);
  }
}

async function sendMessages() {
  const queue = [...new Set(chatIds)];

  const concurrency = 5;

  await Promise.all(Array.from({ length: concurrency }, () => worker(queue)));

  // 💾 ذخیره گزارش نهایی
  fs.writeFileSync("report.json", JSON.stringify(report, null, 2));

  console.log("📄 Report saved to report.json");
  console.log(`Success: ${report.success.length}`);
  console.log(`Failed: ${report.failed.length}`);
  console.log(`Retry: ${report.retry.length}`);
}

sendMessages();
