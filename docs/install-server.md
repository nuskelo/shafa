# Бот на сервері: встановлення і перехід з Mac (OPS-005)

Для Власника. Команди виконуєш ти; Claude на сервер не заходить. Секрети (токен бота, ключі) вводиш
**сам у `nano`** і ніколи не пересилаєш у чат. Скрипти, які тут запускаються, лежать у `ops/server/`.

Схема: **бот живе на сервері** (опитує Telegram, публікує в канал, складає пости в «вихідну чергу»),
**комп** (спершу твій Mac, потім її Windows) раз на 10 хв забирає чергу через SSH, а сайт, звіт, тривоги
й робота веде сам. Нових відкритих портів немає: комп ходить на сервер по SSH ключем, який уміє лише
команди «give N» / «ack N».

Скрізь нижче `IP` — адреса сервера, `ubuntu` — його користувач.

## 1. Перевірка сервера (чого ми не знаємо без тебе)

На сервері (`ssh ubuntu@IP`):

```sh
lsb_release -ds; uname -m; python3 --version; python3.12 --version 2>&1 | head -n 1
timedatectl | head -n 4; free -h | head -n 3; df -h / | tail -n 1; swapon --show; ssh -V
```

Надішли Архітектору цей вивід (секретів у ньому немає). **Потрібен Python 3.12** (Ubuntu 24.04 має його
в пакетах). Якщо `python3.12` немає і Ubuntu старіший — **зупинись** і напиши Архітектору, рішення за ним.
Залежності проєкту мають готові колеса для Linux x86_64 і aarch64 (перевірено), компілятор не потрібен.

## 2. Підготовка системи (один раз)

```sh
sudo timedatectl set-timezone Europe/Kyiv        # обов'язково: дата поста рахується за часом сервера
sudo apt update && sudo apt install -y git sqlite3 python3.12 python3.12-venv
```

Swap (на 1 ГБ пам'яті `pip` без нього може впасти). Якщо `swapon --show` у кроці 1 порожній:

```sh
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Перевірка: `timedatectl | grep "Time zone"` → `Europe/Kyiv`; `free -h` → Swap 2.0G.

## 3. Код і встановлення

Репозиторій публічний, ключів не треба:

```sh
git clone https://github.com/nuskelo/shafa.git ~/shafa
sh ~/shafa/ops/server/install.sh 2>&1 | tail -n 20
```

Скрипт: створює `.venv` і ставить пакет **без браузера** Playwright; створює теку даних `~/shafa-data`
(права 700), порожній `.env` (права 600, лише назви) і порожню базу; ставить службу `shafa-bot`
(**не вмикає її**) і таймер нічної копії; кладе помічник `~/shafa-cli`. Повторний запуск безпечний.

## 4. Налаштування `.env`

```sh
nano ~/shafa-data/.env
ls -l ~/shafa-data/.env        # має бути -rw------- (600)
```

Значення переписуй із `.env` на Mac руками (не надсилай їх мені). `SHAFA_ROOT_DIR` уже стоїть.

| Назва | Що |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен бота (секрет) |
| `TELEGRAM_BOT_CHANNEL_ID` | канал, **куди бот публікує** |
| `TELEGRAM_CHANNEL_ID` | id каналу Продавчині (для назв папок) |
| `TELEGRAM_SELLER_CHAT_ID`, `TELEGRAM_OWNER_CHAT_ID` | її чат і твій |
| `SHAFA_ASK_BUTTON`, `SHAFA_SITE_BUTTON`, `SHAFA_AI_WAIT_DAYS`, `SHAFA_SITE_WAIT_DAYS` | як на Mac (впливають на текст бота) |

**Не додавай** `ANTHROPIC_API_KEY`, `SITE_INTEGRATION_KEY`, `SHAFA_SITE_SEND` і будь-що від робота: на
сервері їх бути не має.

Для репетиції (розділ 7) тут мають стояти **тестові** значення: токен тестового бота і тестовий канал.
Справжні — лише перед переходом (розділ 8, крок 0).

## 5. Ключ обміну і відбиток сервера

Ключ потрібен лише для обміну: оболонки він не дає. На **Mac** (у тому вікні, де `SHAFA_DATA_DIR`
виставлена, як у `docs/install-mac.md`):

```sh
export SHAFA_DATA_DIR="$HOME/shafa-viktoria"
ssh-keygen -t ed25519 -N "" -C shafa-exchange-mac -f "$SHAFA_DATA_DIR/bot-server-key"
chmod 600 "$SHAFA_DATA_DIR/bot-server-key"
scp "$SHAFA_DATA_DIR/bot-server-key.pub" ubuntu@IP:~/exchange-mac.pub
ssh-keyscan -t ed25519 IP > "$SHAFA_DATA_DIR/bot-server-known_hosts"
ssh-keygen -lf "$SHAFA_DATA_DIR/bot-server-known_hosts"
```

Останній рядок показує відбиток сервера. **Звір його** з відбитком, який покаже сам сервер (на сервері:
`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`). Збіглись — іди далі; ні — **зупинись**.

На сервері додай ключ з примусовою командою і прибери файл:

```sh
sh ~/shafa/ops/server/install.sh --add-key ~/exchange-mac.pub && rm ~/exchange-mac.pub
tail -n 1 ~/.ssh/authorized_keys | cut -c1-140
```

Рядок має починатися з `restrict,command="env SHAFA_DATA_DIR=…outbox --ssh…"`. Перевірка з Mac:

```sh
S="ssh -i $SHAFA_DATA_DIR/bot-server-key -o BatchMode=yes -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$SHAFA_DATA_DIR/bot-server-known_hosts ubuntu@IP"
$S "ack 0"       # → ok 0
$S "ls"          # → Не вдалося: Дозволено лише «give N» або «ack N»  (оболонки немає)
```

Її Windows-ноутбук отримає **свій** ключ окремим тікетом (OPS-006); ключ Mac туди не копіюємо.

## 6. Служба, журнал, копії, оновлення

Служба `shafa-bot` після встановлення вимкнена. Вмикається кроками 7–8.

```sh
sudo systemctl status shafa-bot --no-pager | head -n 15     # стан
journalctl -u shafa-bot -n 50 --no-pager                    # журнал (stdout бота)
journalctl -u shafa-bot -f                                  # дивитись наживо (Ctrl+C — вийти)
systemctl list-timers shafa-backup.timer --no-pager         # коли наступна копія
ls -l ~/shafa-data/backups | tail -n 8                      # копії: щоночі о 03:30, 7 останніх
```

Служба сама піднімається після збою через 10 с (`Restart=always`) і не бере більше 400 МБ пам'яті.
Якщо в журналі щосекунд-десять повторюється одна й та сама помилка — це `.env` чи права бота:
`~/shafa-cli bot --db ~/shafa-data/shafa.sqlite --check`.

**Відновити базу з копії** (служба вимкнена на час): 
`sudo systemctl stop shafa-bot && rm -f ~/shafa-data/shafa.sqlite-wal ~/shafa-data/shafa.sqlite-shm && cp ~/shafa-data/backups/shafa-РРРР-ММ-ДД.sqlite ~/shafa-data/shafa.sqlite && sudo systemctl start shafa-bot`.
Копії лежать на тому самому диску: раз на тиждень можна забирати на Mac
(`scp ubuntu@IP:~/shafa-data/backups/shafa-*.sqlite ~/Desktop/`). Фото постів копіями не покриті: вони
вже у комп'ютері після обміну, а на сервері лежать 14 днів після підтвердження.

**Оновлення** коду (коли Архітектор скаже, що вийшла нова версія; схема бази сервера й компа має
збігатися — це він перевіряє):

```sh
cd ~/shafa && git pull --ff-only 2>&1 | tail -n 3
.venv/bin/python -m pip install --quiet -e . && sudo systemctl restart shafa-bot
journalctl -u shafa-bot -n 10 --no-pager
```

`git pull` не пройшов (`--ff-only`) — не тисни далі, напиши Архітектору.

## 7. Репетиція (до переходу): тестовий бот, тестовий канал, окрема тека на Mac

Мета — пройти весь ланцюг «пост → сервер → черга → Mac» **без справжніх даних і без простою**.
Чому окремо: (а) два опитувачі з одним токеном б'ються (409) — тому **тестовий бот**; (б) номер поста в
тестовому каналі може збігтися зі справжнім постом у базі Mac (`ON CONFLICT DO NOTHING` мовчки його
проігнорує) — тому **окрема тека даних** на Mac; справжню базу репетиція не чіпає.

Потрібно (одноразово): тестовий бот у `@BotFather` (`/newbot`, токен — лише в `.env` сервера), доданий
адміном у тестовий канал і в його групу обговорення (як справжній бот), твій чат із ним (`/start`).

1. **Сервер**, `nano ~/shafa-data/.env`: тестові `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_CHANNEL_ID`;
   `TELEGRAM_SELLER_CHAT_ID` = `TELEGRAM_OWNER_CHAT_ID` = твій id. Перевірка:
   `~/shafa-cli bot --db ~/shafa-data/shafa.sqlite --check` → «права в каналі є».
2. **Сервер**: `sudo systemctl enable --now shafa-bot; sleep 5; journalctl -u shafa-bot -n 5 --no-pager`
   → «Бот працює на сервері…».
3. **Mac**, окрема тека (ключ і відбиток — ті самі, що в розділі 5; токена тут немає, тож звіт, тривоги й
   сайт з цієї теки не надсилаються):
   ```sh
   export SHAFA_DATA_DIR="$HOME/shafa-ops005-test"
   mkdir -p "$SHAFA_DATA_DIR/root"
   cp "$HOME/shafa-viktoria/bot-server-key" "$HOME/shafa-viktoria/bot-server-known_hosts" "$SHAFA_DATA_DIR/"
   printf 'SHAFA_ROOT_DIR=%s\nSHAFA_BOT_SERVER=ubuntu@IP\n' "$SHAFA_DATA_DIR/root" > "$SHAFA_DATA_DIR/.env"
   cd ~/shafa && nohup .venv/bin/python -m shafa app --no-browser --port 8766 > "$SHAFA_DATA_DIR/app.log" 2>&1 &
   sleep 5; tail -n 5 "$SHAFA_DATA_DIR/app.log"
   ```
   Якщо справжня програма на Mac уже працює — це інша тека даних, замки різні, конфлікту немає.
4. У тестового бота надішли **з свого чату** фото з підписом (наприклад «Тест OPS-005, 500 грн, розмір 38»),
   натисни «Опублікувати» → пост у **тестовому каналі**.
5. На Mac за ≤ 10 хв (обмін при старті й кожні 10 хв; швидше — перезапустити `app` у тій самій теці):
   у `$SHAFA_DATA_DIR/root/` є тека з номером поста і фото; `tail -n 5 "$SHAFA_DATA_DIR/bot-sync.log"` —
   рядок про цей пост; `sqlite3 "$SHAFA_DATA_DIR/shafa.sqlite" "select post_id, source from posts"` →
   `…|bot`. Відредагуй текст поста в тестовому каналі — через ≤ 10 хв `posts.text` оновлюється.
6. **Прибирання**. Mac: `quit` програми в тестовій теці (`.venv/bin/python -m shafa quit` з тією ж
   `SHAFA_DATA_DIR`), потім `rm -rf ~/shafa-ops005-test`. Сервер — стерти все, крім `.env` і копій
   (інакше тестова черга поїде в справжню базу Mac):
   ```sh
   sudo systemctl disable --now shafa-bot
   find ~/shafa-data -mindepth 1 -maxdepth 1 ! -name .env ! -name backups -exec rm -rf {} +
   sh ~/shafa/ops/server/install.sh 2>&1 | tail -n 3      # знову створить root і порожню базу
   ```

Результат репетиції (пункти 2, 4, 5 — ✅/❌, обрізаний вивід журналу) — Архітектору. Без «✅» — не
переходимо.

## 8. Перехід Mac → сервер (чекліст)

**Умови** (усі — «так», інакше не починай):

- [ ] Репетиція (розділ 7) пройшла, сервер після неї почищений (крок 6 репетиції).
- [ ] Архітектор сказав «переходимо».
- [ ] Продавчиня знає: бот хвилин 15 не відповідає; повідомлення в цей час **не втрачаються** (Telegram
      тримає їх до 24 год).
- [ ] У боті **немає чернеток** (повідомлення «Чернеток: N» немає): опублікуй чи скасуй їх. Чернетки
      переїжджають на сервер зі збереженими прев'ю, але краще починати з чистого.
- [ ] Сесія робота на Mac закінчена (немає «Публікує…» на панелі).
- [ ] Копія: `cp "$HOME/shafa-viktoria/shafa.sqlite" "$HOME/shafa-viktoria/shafa.sqlite.до-сервера"`.

**Кроки.** Спершу — **справжні** значення: на сервері `nano ~/shafa-data/.env` (справжні токен і
`TELEGRAM_BOT_CHANNEL_ID`, решта як у розділі 4). Далі:

| # | Де | Команда | Чекаємо |
|---|---|---|---|
| 1 | Mac | `export SHAFA_DATA_DIR="$HOME/shafa-viktoria"; cd ~/shafa; .venv/bin/python -m shafa quit` | програма й бот зупинені |
| 2 | Mac | `.venv/bin/python -m shafa bot-pack --db "$SHAFA_DATA_DIR/shafa.sqlite" --out ~/bot-pack.zip` | текст-підсумок; якщо бот ще працює — відмова, це нормально, повтори крок 1 |
| 3 | Mac | `scp ~/bot-pack.zip ubuntu@IP:~/bot-pack.zip` | файл на сервері |
| 4 | сервер | `~/shafa-cli bot-unpack --db ~/shafa-data/shafa.sqlite --zip ~/bot-pack.zip 2>&1 \| tail -n 15` | підсумок без помилок; скинь його Архітектору |
| 5 | сервер | `~/shafa-cli bot --db ~/shafa-data/shafa.sqlite --check` | «права в каналі є» |
| 6 | сервер | `sudo systemctl enable --now shafa-bot; sleep 5; journalctl -u shafa-bot -n 10 --no-pager` | «Бот працює на сервері…», без повторів помилки |
| 7 | Mac | `echo 'SHAFA_BOT_SERVER=ubuntu@IP' >> "$SHAFA_DATA_DIR/.env"` і запусти програму як завжди | на панелі «Бот: на сервері, на зв'язку N хв тому» |
| 8 | Mac | `tail -n 5 "$SHAFA_DATA_DIR/bot-sync.log"` | «обмін» без помилок |

Після кроку 6 бот відповідає Продавчині вже з сервера — їй можна написати, що все працює.

**Перевірка без штучних постів у справжньому каналі:** її перший справжній пост після переходу має
з'явитись на Mac (папка в Головній, пост у базі) протягом ≤ 10 хв; правка його тексту в каналі теж
доїжджає. Повідомлення, що чекали в Telegram, бот забирає сам; повтор уже оброблених оновлень ігнорується.

**Відкат** (поки з сервера не пішло нічого справжнього, тобто одразу після переходу):

1. Сервер: `sudo systemctl disable --now shafa-bot`.
2. Mac: прибрати рядок `SHAFA_BOT_SERVER=…` з `$SHAFA_DATA_DIR/.env`, запустити програму — бот знову локальний,
   Telegram сам віддасть непідтверджене.
3. Якщо сервер встиг щось опублікувати чи прийняти — **не відкочуй сам**, напиши Архітектору: дані треба
   повернути на Mac (`bot-pack` на сервері → `bot-unpack` на Mac), інакше з'являться дублі.

## 9. Якщо щось не так

| Симптом | Що робити |
|---|---|
| У журналі повторюється «Немає … у .env» | допиши значення в `~/shafa-data/.env` (назва — у тексті помилки) |
| «409 / другий бот» у журналі | бот ще працює на Mac або інший процес з цим токеном: на Mac `quit` |
| На панелі Mac «зв'язку немає» | на Mac `$S "ack 0"` (розділ 5): якщо не `ok 0` — ключ, відбиток чи сервер недоступний |
| Службу вбито за пам'ять | `journalctl -u shafa-bot -n 50`, потім Архітектору; swap у розділі 2 має бути |
| `install.sh`: «неприпустимі символи» | шляхи — лише латиниця, цифри, `_ . / -` |
