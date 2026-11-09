# Прокси Bot API: Cloudflare Worker

Зачем. В сетях, где `api.telegram.org` недоступен напрямую, ни уведомления конвейера, ни
бот-команды не работают: каждый тик падает на таймауте. Hermes-ядро умеет ходить через
прокси (`TELEGRAM_API_BASE_URL` в `.env`), конвейер теперь умеет так же. Воркер Cloudflare
работает релеем: принимает запрос, отправляет его в Telegram, отдаёт ответ как есть.

Что подменяется. Bot API отвечает на путях `/bot<токен>/<метод>` и `/file/bot<токен>/…`;
воркер отдаёт те же пути, поэтому меняется только хост:

```
https://api.telegram.org/bot123:ABC/sendMessage
        ↓
https://<воркер>/bot123:ABC/sendMessage
```

## 1. Развернуть воркер

Cloudflare → Workers & Pages → Create → Worker, вставить код, Deploy. Воркер не открытый
релей: он пропускает только токены из секрета `BOT_TOKENS`, остальное — `404`.

```js
/**
 * tg-botapi-proxy — обратный прокси Telegram Bot API на Cloudflare Workers.
 *
 * Маршруты (совместимы с python-telegram-bot base_url/base_file_url):
 *   /bot<TOKEN>/<method>        → https://api.telegram.org/bot<TOKEN>/<method>
 *   /file/bot<TOKEN>/<path>     → https://api.telegram.org/file/bot<TOKEN>/<path>
 *
 * Защита: пропускаются только токены из секрета BOT_TOKENS (список через запятую,
 * пробел или перевод строки). Без совпадения — 404, а не открытый прокси.
 */
const TG_ORIGIN = "https://api.telegram.org";

function allowedTokens(env) {
  return [env.BOT_TOKENS, env.BOT_TOKEN]
    .filter(Boolean)
    .join(",")
    .split(/[\s,]+/)
    .map((t) => t.trim())
    .filter(Boolean);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const tokens = allowedTokens(env);

    if (!tokens.length) {
      return new Response("BOT_TOKENS secret is not set", { status: 500 });
    }

    const isAllowed = tokens.some(
      (t) => url.pathname.startsWith(`/bot${t}/`) || url.pathname.startsWith(`/file/bot${t}/`),
    );
    if (!isAllowed) {
      return new Response("not found", { status: 404 });
    }

    const target = TG_ORIGIN + url.pathname + url.search;
    const init = {
      method: request.method,
      headers: request.headers,
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
      redirect: "manual",
    };

    let upstream;
    try {
      upstream = await fetch(target, init);
    } catch (err) {
      return new Response(`upstream error: ${err}`, { status: 502 });
    }

    // Заголовки апстрима отдаём как есть: тело не распаковываем, content-encoding
    // трогать нельзя — иначе клиент получит сжатые байты без пометки.
    return new Response(upstream.body, { status: upstream.status, headers: upstream.headers });
  },
};
```

Через wrangler:

```bash
npm i -g wrangler
wrangler deploy worker.js --name tg-botapi-proxy
wrangler secret put BOT_TOKENS     # токены ботов через запятую: 123:AAA,456:BBB
```

Секрет меняется и через API, не пересоздавая воркер:

```bash
curl -X PUT "https://api.cloudflare.com/client/v4/accounts/<account_id>/workers/scripts/tg-botapi-proxy/secrets" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" -H "Content-Type: application/json" \
  --data '{"name":"BOT_TOKENS","text":"123:AAA,456:BBB","type":"secret_text"}'
```

`BOT_TOKENS` заменяется целиком: перечисляйте **все** токены, которым нужен прокси, иначе
пропадёт доступ у остальных ботов.

## 2. Включить в конвейере

В `.env` профиля Hermes (там же, где `STEP_PIPELINE_TELEGRAM_BOT_TOKEN`):

```bash
STEP_PIPELINE_TELEGRAM_API_BASE=https://<воркер>/bot
```

База оканчивается на `/bot` — так её строит и шлюз Hermes (`TELEGRAM_API_BASE_URL`), и
python-telegram-bot: путь `<база><токен>/<метод>` даёт `/bot<токен>/<метод>`. Ключ
конвейера сильнее общего: если прокси уже настроен для шлюза, конвейер подхватит его сам.
Допустима и база без `/bot` (`https://<воркер>`) — префикс допишется, дубля не будет.

## 3. Проверить

```bash
hermes pipeline notify-test            # тестовое сообщение владельцу
hermes pipeline doctor                 # строка «Telegram» должна быть без ✗
```

Что означают отказы:

- `404 not found` — токен бота не входит в `BOT_TOKENS` воркера (или путь собран не по
  шаблону `/bot<токен>/…`).
- `500 BOT_TOKENS secret is not set` — секрет не задан.
- `403 … error code: 1010` — Cloudflare заблокировал User-Agent клиента. Конвейер и
  ядро Hermes шлют свой UA, `curl` и `python-urllib` по умолчанию Cloudflare режет;
  проверяйте запросы с `-A` или из самого конвейера.
- `502 upstream error: …` — воркер жив, но из Cloudflare не достучался до Telegram.
- `401 Unauthorized` — запрос дошёл до Telegram и отвергнут токеном.

## Границы

- Воркер ничего не хранит и не логирует: релей и есть весь код.
- Файлы (`getFile`) конвейер не скачивает; для ядра Hermes есть отдельный ключ
  `TELEGRAM_API_BASE_FILE_URL` (`https://<воркер>/file/bot`), тот же воркер.
- Уведомления по-прежнему уходят только в чат из настроек (`STEP_PIPELINE_TELEGRAM_CHAT_ID`).
- Движок обязан работать без установленного плагина, поэтому адрес читается из
  окружения, а не из базы настроек: `gate.py` и `telegram.py` смотрят одни и те же ключи.
