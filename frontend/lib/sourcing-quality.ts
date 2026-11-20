export interface QualityListing {
  itemId: string;
  itemUrl: string;
  title: string;
  price: number | null;
  detail_sold_count?: number;
  sold_count?: number;
  detail_stock?: number;
  sold_out?: boolean;
  view_count?: number;
  want_count?: number;
  browse_count?: number;
  want_source?: string;
  want_stale?: boolean;
}

export interface QualityQuote {
  title: string;
  price: number;
}

export type MarketConfidence = 'high' | 'medium' | 'low' | 'invalid';

export interface ListingAssessment {
  listing: QualityListing;
  comparable: boolean;
  priceSane: boolean;
  sellable: boolean;
  reasons: string[];
  activityScore: number;
  sold: number | null;
  want: number | null;
  browse: number | null;
}

export interface MarketEvidence {
  confidence: MarketConfidence;
  eligibleForProfit: boolean;
  benchmark: QualityListing | null;
  benchmarkBasis: 'activity' | 'median' | '';
  comparableCount: number;
  validCount: number;
  suspiciousCount: number;
  reasons: string[];
  assessments: ListingAssessment[];
}

const LOW_PRICE_RATIO = 0.45;
const HIGH_PRICE_RATIO = 8;
const PRICE_CONSENSUS_MAX_ADJACENT_MULTIPLE = 2;
export const QUOTE_MAX_AGE_MS = 10 * 60_000;
const MARKET_MAX_AGE_MS = 24 * 60 * 60_000;

const BRAND_ALIASES: string[][] = [
  ['哔哩哔哩', 'b站', 'bilibili'],
  ['优酷', 'youku'],
  ['爱奇艺', 'iqiyi'],
  ['腾讯视频', '腾讯影音'],
  ['芒果tv', '芒果视频'],
  ['网易云音乐', '网易云'],
  ['qq音乐'],
  ['喜马拉雅'],
  ['酷狗音乐'],
  ['qq会员'],
  ['京东e卡', '京东礼品卡'],
  ['星巴克'],
  ['麦当劳'],
  ['美团'],
  ['瑞幸'],
  ['肯德基'],
  ['滴滴'],
];

const CHOICE_RELATION = /或者|任选(?:其一)?|可选|(?:二|三|四|五|六|七|八|九|十)选一|择一|其一/;
const MEMBERSHIP = /vip|会员(?!专享|价|券|折扣)|黑胶/;
const MEMBERSHIP_TIERS = [
  { name: '黄金', pattern: /黄金(?:vip|会员)/ },
  { name: '白金', pattern: /白金(?:vip|会员)/ },
  { name: '星钻', pattern: /星钻(?:vip|会员)/ },
  { name: '大会员', pattern: /大会员/ },
  { name: '超级会员', pattern: /超级(?:影视)?(?:vip|会员)|svip/ },
  { name: '电视版', pattern: /电视版|tv版|云视听小电视/ },
  { name: '酷喵', pattern: /酷喵/ },
];

const NON_PRODUCT_CLAUSES: RegExp[] = [
  /(?:偏远地区|部分地区)?(?:补|加)?(?:运费|邮费|邮资)\s*\d+(?:\.\d+)?\s*元/g,
  /\d+(?:\.\d+)?\s*元(?:运费|邮费|邮资)/g,
  /(?:不支持|非)?7天无理由(?:退换货|退换|退货|退款)?/g,
  /(?:12|24|48|72)\s*(?:小时|h)(?:内)?\s*(?:自动)?(?:发货|到账|充值|处理)/gi,
  /(?:每人|每单|单笔|每号|每个账号)\s*(?:限购|限拍|最多(?:购买|拍下|买)?|仅限)\s*\d+\s*(?:件|个|份|张|单|次)/g,
];

const HOT_QUOTE_HARD_NOISE = [
  /网赚|兼职|副业|日赚|月入|收益|赚钱|爆单|拉新|引流|涨粉|粉丝|人气|流量爆破|代运营/,
  /商品说明|下单必看|拍前必看|专区(?:特别)?提示|说明页|测试商品|自定义测试|补发专用/,
  /免费领取|领取教程|软件安装包|源码|工具包|素材包|资料包|文档模板/,
  /店铺曝光|店铺访客|直播人气/,
  /点餐最低\d+(?:\.\d+)?折|在线点餐|低至\d+(?:\.\d+)?折起|饮品优惠/,
  /红包封面|cp滴滴|特殊铭牌|成长值|认证服务|会员等级代刷|代练/,
];

const HOT_QUOTE_KNOWLEDGE_NOISE = /教程|课程|教学|培训|运营策略|运营课|实战课|进阶教程|蓝海市场|攻略|玩法|思路集/;

const CATEGORY_PRODUCT_CUES: Record<string, RegExp> = {
  美食餐饮: /肯德基|麦当劳|星巴克|瑞幸|汉堡|咖啡|奶茶|早餐|午餐|晚餐|套餐|代金券|兑换券|小食|饮品|甜筒|冰淇淋|外卖|满减/,
  影视娱乐: /爱奇艺|腾讯视频|优酷|芒果|哔哩哔哩|b站|bilibili|视频会员|影视会员|大会员|电影票|影城|云视听/,
  社交会员: /qq|vip|会员/,
  音频音乐: /网易云|qq音乐|酷狗|喜马拉雅|汽水音乐|华为音乐|音乐|听书|乐币/,
  生活服务: /滴滴|美团|花小猪|打车|代驾|出行|话费|流量|外卖|红包|打车券|优惠券/,
  工具软件: /wps|office|微软|adobe|剪映|醒图|夸克|百度网盘|迅雷|会员|vip|激活码|序列号|订阅|专业版|高级版|终身版/,
  游戏虚拟: /点券|游戏币|游戏卡|充值|道具|皮肤|账号|卡密/,
  电商购物: /e卡|购物卡|礼品卡|代金券|面值/,
  娱乐票务: /电影票|影城|演出票|门票/,
  综合权益: /会员|vip|权益|卡密|兑换|代金券|礼品卡|面值/,
};

const ATTRIBUTE_LABELS: Record<string, string> = {
  size: '杯型/尺寸',
  amount: '面额',
};

export function prioritizeRejectionReasons(reasons: string[]) {
  const priceRisks = reasons.filter(reason =>
    reason.startsWith('异常低价：') || reason.startsWith('异常价差：'),
  );
  return [
    ...priceRisks,
    ...reasons.filter(reason => !priceRisks.includes(reason)),
  ];
}

export function hotKeywordMatchesTitle(keyword: string, title: string) {
  const keywordText = normalizeTitle(keyword);
  const titleText = normalizeTitle(title);
  if (!keywordText || !titleText) return false;
  if (titleText.includes(keywordText)) return true;

  const keywordBrands = brandsIn(keyword);
  const titleBrands = brandsIn(title);
  return keywordBrands.size > 0
    && [...keywordBrands].every(brand => titleBrands.has(brand));
}

function advertisedFaceValue(title: string) {
  const values: number[] = [];
  for (const match of normalizeTitle(title).matchAll(
    /(?:面值|面额)[|｜:：]?(\d+(?:\.\d+)?)元|(\d+(?:\.\d+)?)元[·・|｜:：-]?面值/g,
  )) {
    values.push(Number(match[1] ?? match[2]));
  }
  return values.length ? Math.max(...values) : null;
}

function isKnowledgeProduct(keyword: string, category: string) {
  return /^(教程|资料|网盘)$/.test(normalizeTitle(keyword))
    || /教育知识|网盘资料/.test(category);
}

function hasCategoryProductShape(keyword: string, title: string, category: string) {
  const cue = CATEGORY_PRODUCT_CUES[category];
  if (!cue) return true;
  if (cue.test(normalizeTitle(title))) return true;
  return isKnowledgeProduct(keyword, category);
}

export function isUsableHotQuote(
  keyword: string,
  title: string,
  price: number,
  category = '',
) {
  const keywordText = normalizeTitle(keyword);
  const text = normalizeTitle(title);
  if (!text || !Number.isFinite(price) || price <= 0) return false;
  if (HOT_QUOTE_HARD_NOISE.some(pattern => pattern.test(text))) return false;
  if (!isKnowledgeProduct(keyword, category) && HOT_QUOTE_KNOWLEDGE_NOISE.test(text)) {
    return false;
  }
  if (!isKnowledgeProduct(keyword, category) && !hotKeywordMatchesTitle(keyword, title)) {
    return false;
  }
  if (!hasCategoryProductShape(keyword, title, category)) return false;

  const faceValue = advertisedFaceValue(title);
  if (faceValue !== null && faceValue >= 5 && price < faceValue * 0.15) return false;

  const keywordIsMembership = MEMBERSHIP.test(productText(keyword))
    || membershipTiers(keyword).length > 0;
  if (
    keywordIsMembership
    && !MEMBERSHIP.test(productText(title))
    && membershipTiers(title).length === 0
  ) {
    return false;
  }
  if (keywordIsMembership && durationDays(keyword).length === 0) {
    const titleDurations = durationDays(title);
    if (titleDurations.length === 1 && titleDurations[0] === 1) return false;
  }

  const keywordDeclaresCombination = jointPartCount(keyword) > 1 || brandsIn(keyword).size > 1;
  const titleCombinesProducts = jointPartCount(title) > 1 || brandsIn(title).size > 1;
  if (!keywordDeclaresCombination && titleCombinesProducts) return false;

  return Boolean(keywordText);
}

function normalizeTitle(title: string) {
  return title.normalize('NFKC').replace(/\s+/g, '').toLowerCase();
}

function normalizeProductText(title: string) {
  return normalizeTitle(title)
    .replace(/【[^】]*】/g, '')
    .replace(/[+＋&＆/／、,，·•|｜—_-]/g, '');
}

function productText(title: string) {
  let text = normalizeTitle(title);
  for (const clause of NON_PRODUCT_CLAUSES) {
    text = text.replace(clause, '');
  }
  return text;
}

function finiteCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function brandsIn(title: string) {
  const text = normalizeTitle(title);
  return new Set(
    BRAND_ALIASES
      .filter(aliases => aliases.some(alias => text.includes(alias)))
      .map(aliases => aliases[0]),
  );
}

export function hasComparableProductIdentity(quoteTitle: string, listingTitle: string) {
  const quoteBrands = brandsIn(quoteTitle);
  const listingBrands = brandsIn(listingTitle);
  if (quoteBrands.size) {
    if (quoteBrands.size !== listingBrands.size) return false;
    if (![...quoteBrands].every(brand => listingBrands.has(brand))) return false;

    const quoteText = productText(quoteTitle).replace(/[【】\[\]()（）『』]/g, '');
    const listingText = productText(listingTitle).replace(/[【】\[\]()（）『』]/g, '');
    const quoteMembership = MEMBERSHIP.test(quoteText) || membershipTiers(quoteTitle).length > 0;
    const listingMembership = MEMBERSHIP.test(listingText) || membershipTiers(listingTitle).length > 0;

    // 同品牌不等于同商品：音乐人认证不能对标音乐会员，瑞幸通兑券也不能
    // 对标某一款甜点。会员商品继续交给等级/时长检查，非会员商品必须让
    // 上游商品名中的核心词出现在挂牌标题里。
    if (quoteMembership !== listingMembership) return false;
    if (quoteMembership) return true;

    const quoteCore = productCoreText(quoteText, quoteBrands);
    const listingCore = productCoreText(listingText, listingBrands);
    if (!quoteCore || !listingCore) return quoteCore === listingCore;
    return quoteCore.includes(listingCore) || listingCore.includes(quoteCore);
  }
  const quoteText = normalizeProductText(quoteTitle);
  return Boolean(quoteText) && normalizeProductText(listingTitle).includes(quoteText);
}

function productCoreText(text: string, brands: Set<string>) {
  let core = text;
  for (const aliases of BRAND_ALIASES) {
    if (!brands.has(aliases[0])) continue;
    for (const alias of aliases) {
      core = core.replaceAll(normalizeProductText(alias), '');
    }
  }
  core = core
    .replace(/(?:月卡|年卡|季卡|周卡|日卡)/g, '')
    .replace(/\d+(?:天|日)(?:卡)?/g, '')
    .replace(/\d+(?:个)?月/g, '')
    .replace(/\d+年/g, '');
  return core.replace(/[^0-9a-z\u4e00-\u9fff]/g, '');
}

function jointPartCount(title: string) {
  return productText(title)
    .split(/[+&]/)
    .filter(part => part.length > 0)
    .length;
}

function jointComponentSignatures(title: string) {
  return productText(title)
    .split(/[+&]/)
    .filter(part => part.length > 0)
    .map(part => {
      const brands = [...brandsIn(part)].sort();
      return brands.length
        ? JSON.stringify([brands, durationDays(part), membershipTiers(part)])
        : `text:${normalizeProductText(part)}`;
    })
    .sort();
}

function membershipTiers(title: string) {
  const text = productText(title);
  return MEMBERSHIP_TIERS.filter(tier => tier.pattern.test(text)).map(tier => tier.name);
}

function attributeValues(title: string) {
  const text = productText(title);
  const groups = new Map<string, Set<string>>();
  const add = (key: string, value: string) => {
    const values = groups.get(key) ?? new Set<string>();
    values.add(value);
    groups.set(key, values);
  };

  for (const match of text.matchAll(/超大杯|大杯|中杯|小杯/g)) {
    add('size', match[0]);
  }
  for (const match of text.matchAll(/(\d+(?:\.\d+)?)\s*元/g)) {
    add('amount', String(Number(match[1])));
  }
  for (const match of text.matchAll(/[￥¥]\s*(\d+(?:\.\d+)?)/g)) {
    add('amount', String(Number(match[1])));
  }
  for (const match of text.matchAll(
    /(?:\d+(?:\.\d+)?\s*[/／、,，+＋]\s*)+\d+(?:\.\d+)?\s*元/g,
  )) {
    for (const value of match[0].match(/\d+(?:\.\d+)?/g) ?? []) {
      add('amount', String(Number(value)));
    }
  }
  for (const match of text.matchAll(
    /(\d+)\s*(张|杯|份|次|件|个|瓶|包|盒|套|枚|支|根|袋|串)/g,
  )) {
    add(`quantity:${match[2]}`, String(Number(match[1])));
  }
  return groups;
}

function attributeLabel(key: string) {
  if (key.startsWith('quantity:')) return `${key.slice('quantity:'.length)}数`;
  return ATTRIBUTE_LABELS[key] ?? key;
}

function sameValueSet(left: Set<string>, right: Set<string>) {
  return left.size === right.size && [...left].every(value => right.has(value));
}

const CHINESE_DIGITS = '零一二三四五六七八九';
const CHINESE_DURATION_UNITS: Record<string, number> = { 十: 10, 百: 100, 千: 1000 };
const DURATION_CARD_DAYS: Record<string, number> = {
  日卡: 1, 天卡: 1, 单日: 1, '24小时': 1, 周卡: 7, 週卡: 7, 月卡: 30,
  季卡: 90, 季度卡: 90, 半年: 180, 年卡: 365,
};
const DURATION_UNIT_DAYS: Record<string, number> = {
  天: 1, 日: 1, 周: 7, 週: 7, 星期: 7, 月: 30, 年: 365,
};

function durationCount(value: string) {
  if (/^\d+$/.test(value)) return Number(value);
  let total = 0;
  let digit = 0;
  for (const character of value) {
    const unit = CHINESE_DURATION_UNITS[character];
    if (unit) {
      total += (digit || 1) * unit;
      digit = 0;
    } else {
      digit = character === '两' ? 2 : CHINESE_DIGITS.indexOf(character);
    }
  }
  return total + digit;
}

export function durationDays(title: string): number[] {
  const found = new Set<number>();
  const text = productText(title);
  const durations = /(\d+|[零一二两三四五六七八九十百千]+)(?:个)?(天|日|周|週|星期|月|年)(?:卡|会员|体验|有效期)?|日卡|天卡|单日|24小时|周卡|週卡|月卡|季卡|季度卡|半年|年卡/g;
  for (const [label, value, unit] of text.matchAll(durations)) {
    if (!value) {
      found.add(DURATION_CARD_DAYS[label]);
      continue;
    }
    const count = durationCount(value);
    if (count > 0) found.add(unit === '月' && count === 12 ? 365 : count * DURATION_UNIT_DAYS[unit]);
  }
  return [...found].sort((left, right) => left - right);
}

type DeliveryKind = 'direct' | 'code' | 'shared' | 'mixed' | 'unknown';

function deliveryKind(title: string): DeliveryKind {
  const text = normalizeTitle(title);
  const direct = /账号直充|直充|直接充值|自动充值|充值到账/.test(text);
  const code = /卡密|兑换码|激活码|礼品卡|cdk/.test(text);
  const shared = /租号|账号共享|共享账号|合租账号|账号合租|共用账号/.test(text);
  if ([direct, code, shared].filter(Boolean).length > 1) return 'mixed';
  if (direct) return 'direct';
  if (code) return 'code';
  if (shared) return 'shared';
  return 'unknown';
}

function countOf(listing: QualityListing, key: keyof QualityListing): number | null {
  return finiteCount(listing[key]);
}

function activityOf(listing: QualityListing) {
  const sold = countOf(listing, 'detail_sold_count');
  const rawWant = countOf(listing, 'want_count');
  const want = rawWant !== null
    && listing.want_stale !== true
    && (listing.want_source === 'detail' || listing.want_source === 'search')
    ? rawWant
    : null;
  const rawBrowse = countOf(listing, 'browse_count') ?? countOf(listing, 'view_count');
  const browse = rawBrowse !== null && rawBrowse > 0 ? rawBrowse : null;
  return {
    sold,
    want,
    browse,
    score: Math.max(0, sold ?? 0) * 100
      + Math.max(0, want ?? 0) * 5
      + Math.max(0, browse ?? 0) * 0.1,
  };
}

function median(values: number[]) {
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function dominantPriceCluster(assessments: ListingAssessment[]) {
  const sorted = [...assessments].sort((left, right) =>
    left.listing.price! - right.listing.price!
    || left.listing.itemId.localeCompare(right.listing.itemId),
  );
  let splitAt = -1;
  let largestMultiple = PRICE_CONSENSUS_MAX_ADJACENT_MULTIPLE;
  for (let index = 1; index < sorted.length; index += 1) {
    const previous = sorted[index - 1].listing.price!;
    const current = sorted[index].listing.price!;
    const multiple = current / previous;
    if (multiple > largestMultiple) {
      largestMultiple = multiple;
      splitAt = index;
    }
  }
  if (splitAt < 1) return sorted;

  const lower = sorted.slice(0, splitAt);
  const upper = sorted.slice(splitAt);
  return upper.length > lower.length ? upper : lower;
}

export function assessListing(
  quote: QualityQuote,
  listing: QualityListing,
): ListingAssessment {
  const reasons: string[] = [];
  let comparable = hasComparableProductIdentity(quote.title, listing.title);
  if (!comparable) {
    reasons.push('商品对象不一致：品牌、数量或标题中的商品组合不同');
  }

  const quoteJointParts = jointPartCount(quote.title);
  const listingJointParts = jointPartCount(listing.title);
  if (quoteJointParts > 1 || listingJointParts > 1) {
    if (quoteJointParts !== listingJointParts) {
      comparable = false;
      reasons.push(
        `联合商品数量不一致：上游 ${quoteJointParts} 项，挂牌 ${listingJointParts} 项`,
      );
    } else {
      const quoteComponents = jointComponentSignatures(quote.title);
      const listingComponents = jointComponentSignatures(listing.title);
      if (
        quoteComponents.length !== listingComponents.length
        || quoteComponents.some((component, index) => component !== listingComponents[index])
      ) {
        comparable = false;
        reasons.push('联合商品组件不一致：各项品牌、等级、时长或交付对象不同');
      }
    }
  }

  if (CHOICE_RELATION.test(normalizeTitle(listing.title))) {
    comparable = false;
    reasons.push('挂牌含“或者/任选”关系，不能证明交付了全部联合规格');
  }
  if (CHOICE_RELATION.test(normalizeTitle(quote.title))) {
    comparable = false;
    reasons.push('上游包含多个可选规格，单项报价待核实');
  }

  const quoteTiers = membershipTiers(quote.title);
  const listingTiers = membershipTiers(listing.title);
  if (!sameValueSet(new Set(quoteTiers), new Set(listingTiers))) {
    comparable = false;
    reasons.push(`会员等级不一致：上游 ${quoteTiers.join('/') || '未写'}，挂牌 ${listingTiers.join('/') || '未写'}`);
  }

  const quoteAttributes = attributeValues(quote.title);
  const listingAttributes = attributeValues(listing.title);
  for (const key of new Set([...quoteAttributes.keys(), ...listingAttributes.keys()])) {
    const quoteValues = quoteAttributes.get(key) ?? new Set<string>();
    const listingValues = listingAttributes.get(key) ?? new Set<string>();
    if (!sameValueSet(quoteValues, listingValues)) {
      comparable = false;
      const format = (values: Set<string>) => values.size ? [...values].join('/') : '未写';
      reasons.push(
        `${attributeLabel(key)}不一致：上游 ${format(quoteValues)}，挂牌 ${format(listingValues)}`,
      );
    }
  }

  const quoteDurations = durationDays(quote.title);
  const listingDurations = durationDays(listing.title);
  const membership = MEMBERSHIP.test(productText(quote.title)) || MEMBERSHIP.test(productText(listing.title));
  if (quoteDurations.length && listingDurations.length) {
    const quoteSet = new Set(quoteDurations);
    const listingSet = new Set(listingDurations);
    const sameSpec = quoteSet.size === listingSet.size
      && [...quoteSet].every(value => listingSet.has(value));
    if (!sameSpec) {
      comparable = false;
      reasons.push(
        `时长不一致：上游 ${quoteDurations.join('/')} 天，挂牌 ${listingDurations.join('/')} 天`,
      );
    } else if (quoteJointParts === 1 && listingJointParts === 1 && quoteDurations.length > 1) {
      comparable = false;
      reasons.push('存在多种时长，当前挂牌价对应的规格待核实');
    }
  } else if (quoteDurations.length && !listingDurations.length) {
    comparable = false;
    reasons.push('挂牌标题未写明与上游一致的时长');
  } else if (!quoteDurations.length && listingDurations.length) {
    comparable = false;
    reasons.push('上游标题未写明时长，挂牌时长无法证明一致');
  } else if (membership) {
    comparable = false;
    reasons.push('双方会员标题均未写明时长');
  }

  const quoteDelivery = deliveryKind(quote.title);
  const listingDelivery = deliveryKind(listing.title);
  if (
    quoteDelivery !== listingDelivery
    || quoteDelivery === 'mixed'
    || (membership && quoteDelivery === 'unknown')
  ) {
    comparable = false;
    reasons.push(
      quoteDelivery === 'unknown' || listingDelivery === 'unknown'
        ? '交付方式待核实：上游或闲鱼标题未写明交付方式'
        : '交付方式不一致或包含多种方式：请核对直充、卡密和共享账号',
    );
  }

  let sellable = true;
  if (
    listing.sold_out === true
    || (typeof listing.detail_stock === 'number' && Number.isFinite(listing.detail_stock)
      && listing.detail_stock <= 0)
  ) {
    sellable = false;
    reasons.push('挂牌已售罄，不能作为可成交市场参照');
  }

  const upstreamPrice = Number(quote.price);
  const listingPrice = Number(listing.price);
  let priceSane = Number.isFinite(upstreamPrice) && upstreamPrice > 0
    && Number.isFinite(listingPrice) && listingPrice > 0;
  if (priceSane) {
    const ratio = listingPrice / upstreamPrice;
    if (ratio < LOW_PRICE_RATIO) {
      priceSane = false;
      reasons.push(`异常低价：挂牌仅为上游的 ${(ratio * 100).toFixed(1)}%`);
    } else if (ratio > HIGH_PRICE_RATIO) {
      priceSane = false;
      reasons.push(`异常价差：挂牌为上游的 ${ratio.toFixed(1)} 倍`);
    }
  } else {
    reasons.push('价格缺失，无法判断规格与价差');
  }

  const activity = activityOf(listing);
  return {
    listing,
    comparable,
    priceSane,
    sellable,
    reasons,
    activityScore: activity.score,
    sold: activity.sold,
    want: activity.want,
    browse: activity.browse,
  };
}

export function selectMarketEvidence(
  quote: QualityQuote,
  listings: QualityListing[],
): MarketEvidence {
  const assessments = listings
    .filter(listing => listing.itemId && listing.itemUrl && listing.title)
    .map(listing => assessListing(quote, listing));

  const comparable = assessments.filter(item => item.comparable);
  const valid = comparable.filter(item => item.priceSane && item.sellable);
  const suspiciousCount = assessments.length - valid.length;
  if (!valid.length) {
    return {
      confidence: 'invalid',
      eligibleForProfit: false,
      benchmark: null,
      benchmarkBasis: '',
      comparableCount: comparable.length,
      validCount: 0,
      suspiciousCount,
      reasons: comparable.length
        ? ['同标题挂牌全部未通过时长、交付方式或价格合理性检查']
        : ['没有与上游规格对齐的闲鱼挂牌'],
      assessments,
    };
  }

  const active = valid.filter(item =>
    (item.sold !== null && item.sold > 0)
    || (item.want !== null && item.want > 0),
  );
  const consensus = dominantPriceCluster(valid);
  const consensusIds = new Set(consensus.map(item => item.listing.itemId));
  const activeInConsensus = active.filter(item => consensusIds.has(item.listing.itemId));
  let benchmark: ListingAssessment;
  let benchmarkBasis: 'activity' | 'median';
  if (activeInConsensus.length) {
    benchmark = activeInConsensus.sort((left, right) =>
      compareNullableDesc(left.sold, right.sold)
      || compareNullableDesc(left.want, right.want)
      || compareNullableDesc(left.browse, right.browse)
      || left.listing.price! - right.listing.price!,
    )[0];
    benchmarkBasis = 'activity';
  } else if (consensus.length > 1) {
    const midpoint = median(consensus.map(item => item.listing.price!));
    benchmark = [...consensus].sort((left, right) =>
      Math.abs(left.listing.price! - midpoint) - Math.abs(right.listing.price! - midpoint),
    )[0];
    benchmarkBasis = 'median';
  } else {
    benchmark = consensus[0];
    benchmarkBasis = 'median';
  }

  const highActivity = (benchmark.sold ?? 0) >= 3 || (benchmark.want ?? 0) >= 10;
  const someActivity = activeInConsensus.length > 0;
  const confidence: MarketConfidence = highActivity
    ? 'high'
    : someActivity
      ? 'medium'
      : 'low';
  const reasons: string[] = [];
  if (benchmarkBasis === 'activity' && (benchmark.sold ?? 0) <= 0) {
    reasons.push('仅有想要等需求信号，成交待核，未计入可赚商品');
  } else if (benchmarkBasis === 'activity') {
    reasons.push('市场参照按详情成交、想要、浏览依次选择同规格高互动挂牌');
  } else if (consensus.length > 1) {
    reasons.push('缺少成交与想要互动，仅保留多个同规格挂牌的中位价格用于复核');
  } else {
    reasons.push('只有一条零互动同规格挂牌，不能据此确认利润');
  }
  if (consensus.length < valid.length) {
    reasons.push(
      `${valid.length - consensus.length} 条同规格挂牌与价格共识区间差距过大，未用于利润基准`,
    );
  }
  if (suspiciousCount) {
    reasons.push(`${suspiciousCount} 条挂牌因规格或价格异常未进入利润计算`);
  }

  return {
    confidence,
    eligibleForProfit: (benchmark.sold ?? 0) > 0,
    benchmark: benchmark.listing,
    benchmarkBasis,
    comparableCount: comparable.length,
    validCount: valid.length,
    suspiciousCount,
    reasons,
    assessments,
  };
}

interface MarketObservation {
  items: QualityListing[];
  fetchedAt?: string | null;
  error?: string;
  exactSearch?: boolean;
}

export function isFreshSnapshot(value: string | null | undefined, maxAge: number, now: number) {
  if (!value) return false;
  const age = now - Date.parse(value);
  return age >= 0 && age <= maxAge;
}

type TimestampedQuote = QualityQuote & { fetched_at?: string | null; stale?: boolean };

export function isFreshQuote(quote: TimestampedQuote, now: number) {
  return !quote.stale && isFreshSnapshot(quote.fetched_at, QUOTE_MAX_AGE_MS, now);
}

export function selectLowestQuote<T extends TimestampedQuote>(current: T, next: T, now: number): T {
  const currentFresh = isFreshQuote(current, now);
  const nextFresh = isFreshQuote(next, now);
  if (currentFresh !== nextFresh) return nextFresh ? next : current;
  return next.price < current.price ? next : current;
}

export function selectObservedMarketEvidence(
  quote: QualityQuote,
  observations: MarketObservation[],
  refreshUnavailable: boolean,
  now: number,
) {
  const byItemId = new Map<string, { listing: QualityListing; observation: MarketObservation }>();
  for (const observation of observations) {
    const fetchedAt = Date.parse(observation.fetchedAt ?? '') || 0;
    for (const listing of observation.items) {
      const previous = byItemId.get(listing.itemId);
      const previousTime = Date.parse(previous?.observation.fetchedAt ?? '') || 0;
      if (!previous || fetchedAt > previousTime) {
        byItemId.set(listing.itemId, { listing, observation });
      }
    }
  }
  const observed = [...byItemId.values()].filter(({ listing, observation }) =>
    observation.exactSearch !== false || hasComparableProductIdentity(quote.title, listing.title),
  );
  const fresh = refreshUnavailable ? [] : observed.filter(row =>
    !row.observation.error && isFreshSnapshot(row.observation.fetchedAt, MARKET_MAX_AGE_MS, now),
  );
  const rows = fresh.length ? fresh : observed;
  const items = rows.map(row => row.listing);
  const evidence = selectMarketEvidence(quote, items);
  if (!fresh.length && observed.length) {
    evidence.eligibleForProfit = false;
    evidence.reasons.unshift(refreshUnavailable
      ? '正在刷新或最近一次闲鱼刷新失败，保留上次挂牌供核对'
      : '闲鱼挂牌已过期或读取失败，仅供核对');
  }
  const displayed = evidence.benchmark
    ?? evidence.assessments.reduce<QualityListing | null>((lowest, assessment) => {
      const listing = assessment.listing;
      return !lowest || (listing.price ?? Infinity) < (lowest.price ?? Infinity) ? listing : lowest;
    }, null);
  return {
    evidence,
    items,
    freshBenchmark: Boolean(evidence.benchmark && fresh.length),
    fetchedAt: displayed
      ? byItemId.get(displayed.itemId)?.observation.fetchedAt ?? null
      : null,
  };
}

export function calculatePriceSpread(costCents: number, saleCents: number, expenseCents: number | null = null) {
  const gapCents = saleCents - costCents;
  return {
    gapCents,
    marginPercent: saleCents > 0 ? gapCents * 100 / saleCents : null,
    netCents: expenseCents === null ? null : gapCents - expenseCents,
  };
}

function compareNullableDesc(left: number | null, right: number | null) {
  return (right ?? -1) - (left ?? -1);
}
