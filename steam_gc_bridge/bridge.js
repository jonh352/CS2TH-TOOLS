'use strict';

const readline = require('readline');
const SteamUser = require('steam-user');
const GlobalOffensive = require('globaloffensive');
const {LoginSession, EAuthTokenPlatformType} = require('steam-session');

const APP_ID = 730;
const GC_CONNECT_TIMEOUT_MS = 75_000;
const CRAFT_TIMEOUT_MS = 60_000;
const LOGIN_TIMEOUT_MS = 180_000;

function emit(event, payload = {}) {
  process.stdout.write(`${JSON.stringify({event, ...payload})}\n`);
}

function safeMessage(error) {
  return String(error && error.message ? error.message : error || '未知错误');
}

function itemId(item) {
  const value = item && (item.id ?? item.itemid);
  return value == null ? '' : String(value);
}

function isStatTrak(item) {
  return Number(item.quality) === 9 || item.kill_eater_value !== undefined;
}

function craftCheckSummary(checked) {
  const kind = checked.statTrak ? 'StatTrak™' : '普通';
  return `本地复核：${checked.selected.length}/${checked.selected.length} 件均存在，rarity=${checked.rarity}，quality=${checked.qualities.join(',')}，类型=${kind}，发送配方=${checked.recipe}`;
}

function cooldownTimestamp(item) {
  const raw = item && item.tradable_after;
  if (!raw) return 0;
  if (raw instanceof Date) return raw.getTime();
  if (typeof raw === 'number') return raw < 10_000_000_000 ? raw * 1000 : raw;
  const parsed = new Date(raw).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatCooldownTime(timestamp) {
  return new Date(timestamp).toLocaleString('zh-CN', {
    hour12: false,
    timeZone: 'Asia/Shanghai',
  });
}

function validateCraftItems(inventory, requestedIds) {
  if (!Array.isArray(requestedIds) || ![5, 10].includes(requestedIds.length)) {
    throw new Error('一键汰换仅支持 10 件材料或 5 件隐秘级材料');
  }
  const unique = new Set(requestedIds.map(String));
  if (unique.size !== requestedIds.length) {
    throw new Error('配方中存在重复的 Steam 资产 ID');
  }
  const byId = new Map((inventory || []).map((item) => [itemId(item), item]));
  const missing = requestedIds.filter((id) => !byId.has(String(id)));
  if (missing.length) {
    throw new Error(`Steam 游戏库存中缺少 ${missing.length} 件指定材料，请刷新库存后重试`);
  }
  const selected = requestedIds.map((id) => byId.get(String(id)));
  if (selected.some((item) => item.casket_id)) {
    throw new Error('指定材料中有物品位于库存组件内，请先取出');
  }
  const qualities = [...new Set(selected.map((item) => Number(item.quality)))];
  if (qualities.some((quality) => !Number.isInteger(quality))) {
    throw new Error('部分材料缺少 CS2 品质类型，无法安全执行汰换');
  }
  const now = Date.now();
  const cooling = selected
    .map((item, index) => ({item, index, timestamp: cooldownTimestamp(item)}))
    .filter((entry) => entry.timestamp > now);
  if (cooling.length) {
    const details = cooling.map((entry) => {
      const id = itemId(entry.item);
      return `第 ${entry.index + 1} 件（ID …${id.slice(-6)}）至 ${formatCooldownTime(entry.timestamp)}`;
    });
    throw new Error(
      `有 ${cooling.length} 件材料仍在交易保护/CD 中，当前无法汰换：\n${details.join('\n')}`,
    );
  }
  const rarity = Number(selected[0].rarity);
  if (!Number.isInteger(rarity) || rarity < 1 || rarity > 6) {
    throw new Error('无法识别材料的汰换品质');
  }
  if (selected.some((item) => Number(item.rarity) !== rarity)) {
    throw new Error(`${selected.length} 件材料的品质不一致，已阻止汰换`);
  }
  if (selected.length === 5 && rarity !== 6) {
    throw new Error('五合一仅支持 5 件隐秘级材料');
  }
  const statTrak = isStatTrak(selected[0]);
  if (selected.some((item) => isStatTrak(item) !== statTrak)) {
    throw new Error('普通与 StatTrak™ 材料不能混合汰换');
  }
  return {
    selected,
    recipe: rarity - 1 + (statTrak ? 10 : 0),
    rarity,
    qualities,
    statTrak,
  };
}

function authenticateWithQr() {
  return new Promise(async (resolve, reject) => {
    const session = new LoginSession(EAuthTokenPlatformType.SteamClient, {
      machineFriendlyName: 'CS2TH 本地一键汰换',
    });
    session.loginTimeout = LOGIN_TIMEOUT_MS;
    let finished = false;
    const fail = (error) => {
      if (finished) return;
      finished = true;
      reject(error instanceof Error ? error : new Error(safeMessage(error)));
    };
    session.on('authenticated', () => {
      if (finished) return;
      finished = true;
      resolve({
        refreshToken: session.refreshToken,
        steamId: String(session.steamID || ''),
        accountName: String(session.accountName || ''),
      });
    });
    session.on('remoteInteraction', () => {
      emit('status', {message: '已扫码，请在 Steam 手机 App 中确认登录'});
    });
    session.on('timeout', () => fail(new Error('Steam 扫码登录已超时')));
    session.on('error', fail);
    try {
      const start = await session.startWithQR();
      emit('qr', {url: start.qrChallengeUrl});
      emit('status', {message: '请使用 Steam 手机 App 扫描二维码并确认'});
    } catch (error) {
      fail(error);
    }
  });
}

function authenticateWithCredentials(input) {
  return new Promise(async (resolve, reject) => {
    const session = new LoginSession(EAuthTokenPlatformType.SteamClient, {
      machineFriendlyName: 'CS2TH 本地一键汰换',
    });
    session.loginTimeout = LOGIN_TIMEOUT_MS;
    let finished = false;
    const fail = (error) => {
      if (finished) return;
      finished = true;
      reject(error instanceof Error ? error : new Error(safeMessage(error)));
    };
    session.on('authenticated', () => {
      if (finished) return;
      finished = true;
      resolve({
        refreshToken: session.refreshToken,
        steamId: String(session.steamID || ''),
        accountName: String(session.accountName || ''),
      });
    });
    session.on('remoteInteraction', () => {
      emit('status', {message: '请在 Steam 手机 App 中确认本次登录'});
    });
    session.on('timeout', () => fail(new Error('Steam 账密登录已超时')));
    session.on('error', fail);
    try {
      emit('status', {message: '正在使用账号、密码和 Steam Guard 令牌登录…'});
      const started = await session.startWithCredentials({
        accountName: String(input.accountName || ''),
        password: String(input.password || ''),
        steamGuardCode: String(input.steamGuardCode || ''),
      });
      if (started && started.actionRequired) {
        emit('status', {message: '登录资料已提交，正在等待 Steam 验证…'});
      }
    } catch (error) {
      fail(error);
    }
  });
}

async function authenticate(input) {
  if (String(input.authMode || '').toLowerCase() === 'credentials') {
    return authenticateWithCredentials(input);
  }
  return authenticateWithQr();
}

function craftWithToken(input, refreshToken) {
  return new Promise((resolve, reject) => {
    const client = new SteamUser({
      autoRelogin: false,
      renewRefreshTokens: true,
      dataDirectory: input.profileDataDir,
    });
    const cs2 = new GlobalOffensive(client);
    let finished = false;
    let loggedOn = false;
    let craftStarted = false;
    let gcTimer = null;
    let craftTimer = null;

    const cleanup = () => {
      if (gcTimer) clearTimeout(gcTimer);
      if (craftTimer) clearTimeout(craftTimer);
      try {
        client.gamesPlayed([]);
      } catch (_) {}
      try {
        client.logOff();
      } catch (_) {}
    };
    const fail = (error, code = '') => {
      if (finished) return;
      finished = true;
      cleanup();
      const wrapped = error instanceof Error ? error : new Error(safeMessage(error));
      if (code) wrapped.code = code;
      reject(wrapped);
    };
    const succeed = (payload) => {
      if (finished) return;
      finished = true;
      cleanup();
      resolve(payload);
    };

    client.on('refreshToken', (token) => {
      if (token) emit('token', {refreshToken: token});
    });
    client.on('error', (error) => {
      fail(error, loggedOn ? 'STEAM_RUNTIME' : 'STEAM_AUTH');
    });
    client.on('loggedOn', () => {
      loggedOn = true;
      const steamId = String(client.steamID ? client.steamID.getSteamID64() : '');
      if (input.expectedSteamId && steamId !== String(input.expectedSteamId)) {
        fail(
          new Error(`扫码账号与采购批次账号不一致（当前 ${steamId}）`),
          'ACCOUNT_MISMATCH',
        );
        return;
      }
      emit('status', {message: 'Steam 登录成功，正在连接 CS2 游戏服务…'});
      client.gamesPlayed([APP_ID]);
      gcTimer = setTimeout(
        () => fail(new Error('连接 CS2 游戏服务超时，请确认 CS2 未在其他设备运行'), 'GC_TIMEOUT'),
        GC_CONNECT_TIMEOUT_MS,
      );
    });

    cs2.on('error', (error) => fail(error, 'GC_ERROR'));
    cs2.on('connectedToGC', () => {
      if (craftStarted || finished) return;
      if (gcTimer) clearTimeout(gcTimer);
      let checked;
      try {
        checked = validateCraftItems(cs2.inventory, input.assetIds);
      } catch (error) {
        fail(error, 'VALIDATION');
        return;
      }
      craftStarted = true;
      emit('crafting', {
        message: '材料已由 CS2 游戏库存二次核对，正在执行汰换…',
        recipe: checked.recipe,
      });
      craftTimer = setTimeout(
        () => fail(new Error('汰换结果等待超时，请先刷新库存确认结果'), 'CRAFT_TIMEOUT'),
        CRAFT_TIMEOUT_MS,
      );
      cs2.craft(input.assetIds.map(String), checked.recipe);
    });
    cs2.on('craftingComplete', (recipe, itemsGained) => {
      if (!craftStarted || finished) return;
      if (Number(recipe) < 0) {
        fail(
          new Error(
            `CS2 拒绝了本次汰换。Valve 原始返回：recipe=${Number(recipe)}，未提供更具体的失败原因。\n${craftCheckSummary(checked)}\n可能原因：材料属于游戏端不可汰换类型、库存状态刚发生变化，或 CS2 游戏会话冲突。`,
          ),
          'CRAFT_REJECTED',
        );
        return;
      }
      const gainedIds = (itemsGained || []).map(String);
      const inventoryById = new Map((cs2.inventory || []).map((item) => [itemId(item), item]));
      const outputItems = gainedIds.map((assetId) => {
        const item = inventoryById.get(assetId) || {};
        return {
          assetId,
          paintIndex: Number(item.paint_index),
          paintWear: Number(item.paint_wear),
          rarity: Number(item.rarity),
          quality: Number(item.quality),
        };
      });
      succeed({
        steamId: String(client.steamID ? client.steamID.getSteamID64() : ''),
        recipe: Number(recipe),
        outputAssetIds: gainedIds,
        outputItems,
      });
    });

    emit('status', {message: '正在登录 Steam…'});
    try {
      client.logOn({refreshToken});
    } catch (error) {
      fail(error, 'STEAM_AUTH');
    }
  });
}

async function readInput() {
  const rl = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  for await (const line of rl) {
    if (line.trim()) return JSON.parse(line);
  }
  throw new Error('未收到一键汰换参数');
}

async function main() {
  if (process.argv.includes('--self-test')) {
    const normal = validateCraftItems(
      Array.from({length: 10}, (_, index) => ({id: String(index + 1), rarity: 3, quality: 4})),
      Array.from({length: 10}, (_, index) => String(index + 1)),
    );
    const statTrak = validateCraftItems(
      Array.from({length: 10}, (_, index) => ({id: String(index + 11), rarity: 3, quality: 9})),
      Array.from({length: 10}, (_, index) => String(index + 11)),
    );
    const souvenir = validateCraftItems(
      Array.from({length: 10}, (_, index) => ({
        id: String(index + 21),
        rarity: 3,
        quality: index === 0 ? 12 : 4,
      })),
      Array.from({length: 10}, (_, index) => String(index + 21)),
    );
    const covertFive = validateCraftItems(
      Array.from({length: 5}, (_, index) => ({id: String(index + 41), rarity: 6, quality: 4})),
      Array.from({length: 5}, (_, index) => String(index + 41)),
    );
    let cooldownDetected = false;
    try {
      validateCraftItems(
        Array.from({length: 10}, (_, index) => ({
          id: String(index + 31),
          rarity: 3,
          quality: 4,
          tradable_after: index === 0 ? new Date(Date.now() + 60_000) : null,
        })),
        Array.from({length: 10}, (_, index) => String(index + 31)),
      );
    } catch (error) {
      cooldownDetected = safeMessage(error).includes('交易保护/CD');
    }
    if (!cooldownDetected) throw new Error('CD 材料预检未生效');
    emit('ready', {
      steamUser: require('steam-user/package.json').version,
      steamSession: require('steam-session/package.json').version,
      globaloffensive: require('globaloffensive/package.json').version,
      normalRecipe: normal.recipe,
      statTrakRecipe: statTrak.recipe,
      souvenirRecipe: souvenir.recipe,
      covertFiveRecipe: covertFive.recipe,
      cooldownDetected,
    });
    return;
  }
  const input = await readInput();
  let refreshToken = String(input.refreshToken || '');
  if (!refreshToken) {
    const auth = await authenticate(input);
    refreshToken = auth.refreshToken;
    emit('token', {refreshToken, steamId: auth.steamId, accountName: auth.accountName});
  }
  let result;
  try {
    result = await craftWithToken(input, refreshToken);
  } catch (error) {
    if (error && error.code === 'STEAM_AUTH' && input.refreshToken) {
      emit('status', {message: 'Steam 游戏授权已失效，需要重新登录'});
      const auth = await authenticate(input);
      refreshToken = auth.refreshToken;
      emit('token', {refreshToken, steamId: auth.steamId, accountName: auth.accountName});
      result = await craftWithToken(input, refreshToken);
    } else {
      throw error;
    }
  }
  emit('success', result);
}

main()
  .then(() => process.exit(0))
  .catch((error) => {
    emit('error', {message: safeMessage(error), code: String(error && error.code || '')});
    process.exit(1);
  });
