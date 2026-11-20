import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assessListing,
  calculatePriceSpread,
  durationDays,
  hasComparableProductIdentity,
  hotKeywordMatchesTitle,
  isFreshSnapshot,
  isUsableHotQuote,
  prioritizeRejectionReasons,
  selectLowestQuote,
  selectMarketEvidence,
  selectObservedMarketEvidence,
} from './sourcing-quality.ts';

const quote = {
  title: '【账号直充】哔哩哔哩会员月卡+优酷会员月卡',
  price: 26.62,
};

function listing(overrides = {}) {
  return {
    itemId: '9001',
    itemUrl: 'https://www.goofish.com/item?id=9001',
    title: '【账号直充】哔哩哔哩会员月卡+优酷会员月卡',
    price: 31,
    ...overrides,
  };
}

test('month and day durations are different specifications', () => {
  assert.deepEqual(durationDays('月卡'), [30]);
  assert.deepEqual(durationDays('1天体验卡'), [1]);
  const result = assessListing(quote, listing({
    title: `${quote.title} 1天体验`,
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /时长不一致/);
});

test('a year card described as twelve months has one normalized duration', () => {
  assert.deepEqual(durationDays('哔哩哔哩年卡12个月'), [365]);
});

test('numeric and Chinese durations stay distinct from the generic day-card label', () => {
  assert.deepEqual(durationDays('哔哩哔哩大会员30天卡'), [30]);
  assert.deepEqual(durationDays('哔哩哔哩大会员三天卡'), [3]);
  assert.deepEqual(durationDays('哔哩哔哩大会员十天卡'), [10]);
  assert.deepEqual(durationDays('哔哩哔哩大会员七天卡'), [7]);
  assert.deepEqual(durationDays('哔哩哔哩大会员一个月月卡'), [30]);
  assert.equal(assessListing(
    { title: 'B站大会员日卡 账号直充', price: 5 },
    listing({ title: '哔哩哔哩大会员三天卡 账号直充' }),
  ).comparable, false);
  assert.equal(assessListing(
    { title: 'B站大会员月卡 账号直充', price: 20 },
    listing({ title: '哔哩哔哩大会员30天卡 账号直充' }),
  ).comparable, true);
});

test('membership needs a stated duration on both sides', () => {
  const title = '哔哩哔哩大会员 账号直充';
  const result = assessListing({ title, price: 20 }, listing({ title }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /未写明时长/);
});

test('membership tiers require matching title evidence', () => {
  const result = assessListing(
    { title: '爱奇艺黄金VIP月卡 账号直充', price: 10 },
    listing({ title: '爱奇艺白金VIP月卡 账号直充', price: 14, detail_sold_count: 6 }),
  );
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /会员等级不一致/);
});

test('joint membership tiers remain attached to the correct brand', () => {
  const result = assessListing(
    { title: '账号直充 哔哩哔哩大会员电视版月卡+优酷会员月卡', price: 20 },
    listing({ title: '账号直充 哔哩哔哩大会员月卡+优酷会员电视版月卡' }),
  );
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /联合商品组件不一致/);
});

test('a shared account or an unstated delivery method does not match direct recharge', () => {
  const upstream = { title: 'B站大会员月卡 账号直充', price: 10 };
  for (const title of ['哔哩哔哩大会员月卡 租号共享', '哔哩哔哩大会员月卡']) {
    const result = assessListing(upstream, listing({ title, price: 14, detail_sold_count: 6 }));
    assert.equal(result.comparable, false);
    assert.match(result.reasons.join(' '), /交付方式/);
  }
});

test('an explicit listing duration cannot match a quote with no stated duration', () => {
  const result = assessListing(
    { title: '【账号直充】哔哩哔哩会员', price: 2 },
    listing({
      title: '【账号直充】哔哩哔哩会员1天体验卡',
      price: 1,
      detail_sold_count: 20,
    }),
  );
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /时长.*无法证明一致/);
});

test('one yuan month-card claim is rejected by price sanity', () => {
  const result = assessListing(quote, listing({ price: 1 }));
  assert.equal(result.priceSane, false);
  assert.match(result.reasons.join(' '), /异常低价/);
});

test('abnormal price is the first rejection reason shown to the seller', () => {
  assert.deepEqual(
    prioritizeRejectionReasons([
      '挂牌含“或者/任选”关系，不能证明交付了全部联合规格',
      '时长不一致：上游 30 天，挂牌 1 天',
      '异常低价：挂牌仅为上游的 3.8%',
    ]),
    [
      '异常低价：挂牌仅为上游的 3.8%',
      '挂牌含“或者/任选”关系，不能证明交付了全部联合规格',
      '时长不一致：上游 30 天，挂牌 1 天',
    ],
  );
});

test('direct recharge and card code listings are not mixed', () => {
  const result = assessListing(quote, listing({
    title: '哔哩哔哩会员月卡+优酷会员月卡 卡密兑换码',
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /交付方式不一致/);
});

test('brand aliases can appear in a different order without adding another brand', () => {
  assert.equal(
    hasComparableProductIdentity(
      '【账号直充】哔哩哔哩会员月卡+优酷会员月卡',
      '现货 账号直充 优酷会员月卡 / B站会员月卡',
    ),
    true,
  );
  assert.equal(
    hasComparableProductIdentity(
      '哔哩哔哩会员月卡',
      '哔哩哔哩会员月卡+优酷会员月卡',
    ),
    false,
  );
});

test('the same brand cannot compare a certification with a membership', () => {
  assert.equal(
    hasComparableProductIdentity(
      '网易云音乐黑胶会员月卡 账号直充',
      '网易云音乐人认证 账号直充',
    ),
    false,
  );
});

test('the same restaurant brand cannot compare one drink with a generic voucher', () => {
  assert.equal(
    hasComparableProductIdentity(
      '瑞幸生椰拿铁大杯',
      '瑞幸通用通兑券',
    ),
    false,
  );
  assert.equal(
    hasComparableProductIdentity(
      '瑞幸生椰拿铁大杯',
      '瑞幸生椰拿铁大杯 到店券',
    ),
    true,
  );
});

test('hot keyword cards reject tutorials, guides and traffic services', () => {
  for (const [keyword, title, price] of [
    ['肯德基', '闲鱼发布肯德基商品代下单目月入1W+，小白店铺当天出单【课程项目解析】', 0.01],
    ['麦当劳', '麦当劳早餐商品说明，下单必看！当前专区不推荐优先购买', 0.21],
    ['美团', '美团店铺曝光（直接执行）', 0.04],
    ['瑞幸', '瑞幸咖啡直充商品说明，下单必看！', 2.21],
    ['腾讯视频会员', '免费领取腾讯视频会员教程', 0.01],
    ['滴滴', '滴滴隐藏拉新项目，提供入口和玩法教程', 0.01],
    ['滴滴', '微信红包封面 CP滴滴', 1.5],
    ['滴滴', '滴滴 阿奇索失败补发专用自定义测试', 0.1],
    ['肯德基', '肯德基点餐最低5折，全国连锁支持外带送餐', 0.8],
  ]) {
    assert.equal(isUsableHotQuote(keyword, title, price), false, title);
  }
});

test('hot keyword cards retain explicit coupons and product links', () => {
  for (const [keyword, title, price] of [
    ['肯德基', '[餐饮混区]-肯德基-天府椒麻香辣粉', 1.01],
    ['美团', '【美团外卖】满减券天天领（必得20减10~15元，不限量）', 1.2],
    ['滴滴', '【卡密】滴滴-5-10元无门槛-打车卷-看说明', 1.11],
    ['瑞幸', '【自动充值】瑞幸咖啡券包【好运小食】小食曲奇4选1', 2.86],
    ['腾讯视频会员', '【官方CDK】腾讯视频会员3天', 7.8],
  ]) {
    assert.equal(isUsableHotQuote(keyword, title, price), true, title);
  }
});

test('a generic membership keyword cannot use a one-day card as its floor', () => {
  assert.equal(
    isUsableHotQuote('腾讯视频会员', '腾讯视频会员日卡', 3),
    false,
  );
  assert.equal(
    isUsableHotQuote('腾讯视频会员日卡', '腾讯视频会员日卡', 3),
    true,
  );
});

test('Bilibili aliases match the membership product without accepting a day card', () => {
  assert.equal(
    hotKeywordMatchesTitle('哔哩哔哩会员', '哔哩哔哩大会员年卡 兑换卡密'),
    true,
  );
  assert.equal(
    isUsableHotQuote('哔哩哔哩会员', '哔哩哔哩大会员一天 | 24小时内使用', 0.69, '影视娱乐'),
    false,
  );
  assert.equal(
    isUsableHotQuote('哔哩哔哩会员', '【兑换卡密】哔哩哔哩-大会员年卡链接兑换', 105.17, '影视娱乐'),
    true,
  );
});

test('a gift-card face value cannot be compared with a supplier link price', () => {
  assert.equal(
    isUsableHotQuote(
      '星巴克',
      '【渠道链接】星巴克-星礼卡 | 200元·面值 |『兑换』',
      0.69,
      '美食餐饮',
    ),
    false,
  );
  assert.equal(
    isUsableHotQuote('星巴克', '【官方链接】星巴克代金券5元', 4.3, '美食餐饮'),
    true,
  );
});

test('education products remain valid while tool-software ads stay excluded', () => {
  assert.equal(
    isUsableHotQuote(
      '教程',
      'Excel函数实战课程 配套练习资料',
      9.9,
      '教育知识',
    ),
    true,
  );
  assert.equal(
    isUsableHotQuote(
      '办公',
      'AI办公技能大提升，学习AI绘画、视频生成',
      1.01,
      '工具软件',
    ),
    false,
  );
});

test('a single-brand keyword cannot use a joint package as its floor', () => {
  assert.equal(
    isUsableHotQuote('哔哩哔哩会员', '哔哩哔哩会员月卡+优酷会员月卡', 13.48),
    false,
  );
});

test('an extra joint package component cannot benchmark the original package', () => {
  const result = assessListing(quote, listing({
    title: '【账号直充】哔哩哔哩会员月卡+优酷会员月卡+京东PLUS会员月卡',
    price: 55,
    detail_sold_count: 20,
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /联合商品数量不一致/);
});

test('a substituted joint package component cannot benchmark the original package', () => {
  const yearCardQuote = {
    title: '【账号直充】哔哩哔哩会员年卡+WPS超级会员年卡',
    price: 88,
  };
  const result = assessListing(yearCardQuote, listing({
    title: '【账号直充】哔哩哔哩会员年卡+京东PLUS会员年卡',
    price: 99,
    detail_sold_count: 20,
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /联合商品组件不一致/);
});

test('choice relationship wording cannot prove a joint membership', () => {
  const result = assessListing(quote, listing({
    title: '【账号直充】哔哩哔哩一天会员登录，或者优酷',
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /或者\/任选/);
});

test('higher sales listing is used ahead of a cheaper zero-sales listing', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ itemId: '1', price: 27, detail_sold_count: 0, want_count: 0 }),
    listing({ itemId: '2', price: 33, detail_sold_count: 18, want_count: 22 }),
  ]);
  assert.equal(evidence.confidence, 'high');
  assert.equal(evidence.eligibleForProfit, true);
  assert.equal(evidence.benchmark.itemId, '2');
  assert.equal(evidence.benchmarkBasis, 'activity');
});

test('an extreme high-sales price outlier cannot establish surface profit', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({
      itemId: 'normal-price',
      price: 28,
      detail_sold_count: 0,
      want_count: 0,
    }),
    listing({
      itemId: 'high-sales-outlier',
      price: 200,
      detail_sold_count: 100,
      want_count: 0,
      want_source: 'detail',
    }),
  ]);
  assert.equal(evidence.benchmark.itemId, 'normal-price');
  assert.equal(evidence.confidence, 'low');
  assert.equal(evidence.eligibleForProfit, false);
});

test('verified zero sales remain distinct from missing sales', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({
      itemId: 'zero',
      price: 28,
      detail_sold_count: 0,
      want_count: 0,
      browse_count: 0,
    }),
    listing({
      itemId: 'unknown',
      price: 29,
      sold_count: 0,
      want_count: 0,
      browse_count: 0,
    }),
  ]);
  const zero = evidence.assessments.find(item => item.listing.itemId === 'zero');
  const unknown = evidence.assessments.find(item => item.listing.itemId === 'unknown');
  assert.equal(zero?.sold, 0);
  assert.equal(unknown?.sold, null);
});

test('sold-out listings are excluded from the benchmark', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ itemId: 'stockout', price: 30, detail_stock: 0, sold_out: true }),
    listing({ itemId: 'available', price: 31, detail_stock: 2, sold_out: false }),
    listing({ itemId: 'available-2', price: 32, detail_stock: 2, sold_out: false }),
  ]);
  assert.ok(evidence.assessments.find(item => item.listing.itemId === 'stockout')?.reasons.some(
    reason => reason.includes('售罄'),
  ));
  assert.notEqual(evidence.benchmark?.itemId, 'stockout');
});

test('single zero-activity listing cannot establish profit', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ detail_sold_count: 0, want_count: 0, browse_count: 0 }),
  ]);
  assert.equal(evidence.confidence, 'low');
  assert.equal(evidence.eligibleForProfit, false);
});

test('multiple zero-activity listings remain untrusted while keeping a review link', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ itemId: '1', price: 27 }),
    listing({ itemId: '2', price: 31 }),
    listing({ itemId: '3', price: 35 }),
  ]);
  assert.equal(evidence.confidence, 'low');
  assert.equal(evidence.eligibleForProfit, false);
  assert.equal(evidence.benchmark.itemId, '2');
  assert.equal(evidence.benchmarkBasis, 'median');
});

test('browse-only activity cannot establish a trusted profit benchmark', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({
      itemId: 'browse-only',
      price: 31,
      detail_sold_count: 0,
      want_count: 0,
      browse_count: 25,
    }),
  ]);
  assert.equal(evidence.confidence, 'low');
  assert.equal(evidence.eligibleForProfit, false);
});

test('stale list wants cannot establish a trusted profit benchmark', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({
      itemId: 'stale-want',
      price: 40,
      detail_sold_count: 0,
      want_count: 999,
      want_source: 'list',
      want_stale: true,
    }),
  ]);
  assert.equal(evidence.assessments[0].want, null);
  assert.equal(evidence.confidence, 'low');
  assert.equal(evidence.eligibleForProfit, false);
});

test('the suspicious one yuan listing is excluded even beside valid links', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ itemId: 'bait', price: 1, detail_sold_count: 500 }),
    listing({ itemId: 'valid-a', price: 30, detail_sold_count: 0 }),
    listing({ itemId: 'valid-b', price: 31, detail_sold_count: 25 }),
  ]);
  assert.equal(evidence.eligibleForProfit, true);
  assert.ok(evidence.assessments.find(item => item.listing.itemId === 'bait')?.reasons.some(
    reason => reason.includes('异常低价'),
  ));
  assert.notEqual(evidence.benchmark.itemId, 'bait');
});

test('the cached one-day Bilibili or Youku listing cannot benchmark a two-service month card', () => {
  const result = assessListing(quote, listing({
    itemId: '1065404783539',
    title: '哔哩哔哩一天会员登录，或者优酷，下单发帐号 和验证码 哔哩哔哩大会员日卡一天',
    price: 1,
    detail_sold_count: 0,
    want_count: 0,
    browse_count: 0,
  }));
  assert.equal(result.comparable, false);
  assert.equal(result.priceSane, false);
  assert.match(result.reasons.join(' '), /或者\/任选/);
  assert.match(result.reasons.join(' '), /时长不一致/);
  assert.match(result.reasons.join(' '), /异常低价/);
});

test('delivery timing and buyer limits are not treated as product duration or quantity', () => {
  const result = assessListing(quote, listing({
    title: '账号直充 哔哩哔哩会员月卡+优酷会员月卡 24小时自动发货，每人限购1件，不支持7天无理由退款',
    price: 31,
  }));
  assert.equal(result.comparable, true);
  assert.doesNotMatch(result.reasons.join(' '), /时长不一致/);
  assert.doesNotMatch(result.reasons.join(' '), /数量不一致/);
});

test('shipping notes are not treated as product denominations', () => {
  const result = assessListing(quote, listing({
    title: '账号直充 哔哩哔哩会员月卡+优酷会员月卡，偏远地区补运费7元',
    price: 31,
  }));
  assert.equal(result.comparable, true);
  assert.doesNotMatch(result.reasons.join(' '), /面额不一致/);
});

test('a package of Starbucks vouchers cannot benchmark one drink', () => {
  const coffeeQuote = {
    title: '星巴克美式咖啡中杯代金券',
    price: 5,
  };
  const result = assessListing(coffeeQuote, listing({
    title: '星巴克大杯诚意券饮品兑换券5张',
    price: 30,
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /杯型\/尺寸不一致|杯数不一致/);
});

test('a KFC snack cannot benchmark a multi-item meal package', () => {
  const kfcQuote = {
    title: '肯德基夜宵脆皮年糕串1串',
    price: 5,
  };
  const result = assessListing(kfcQuote, listing({
    title: '肯德基四件套汉堡1个+年糕串1串+蛋挞1只+可乐1杯',
    price: 20,
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(','), /个片数|只片数|杯片数|串数|数量/);
});

test('a combined denomination list cannot benchmark one listed denomination', () => {
  const cardQuote = {
    title: '京东E卡200元',
    price: 190,
  };
  const result = assessListing(cardQuote, listing({
    title: '京东E卡面值1/2/5/10/20/50/100/200元',
    price: 190,
  }));
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /面额不一致/);
});

test('a title that lists multiple currency-symbol prices is not a certain SKU', () => {
  const result = assessListing(
    { title: '哔哩哔哩会员月卡', price: 2 },
    listing({
      title: '哔哩哔哩会员月卡 ￥5 哔哩哔哩会员月卡（电视版）￥8',
      price: 5,
      detail_sold_count: 20,
    }),
  );
  assert.equal(result.comparable, false);
  assert.match(result.reasons.join(' '), /面额不一致/);
});

test('boilerplate does not prevent a high-sales exact-spec listing from becoming the benchmark', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({
      itemId: 'low-activity',
      price: 30,
      detail_sold_count: 0,
      want_count: 0,
      browse_count: 2,
    }),
    listing({
      itemId: 'high-sales',
      price: 32,
      title: '账号直充 哔哩哔哩会员月卡+优酷会员月卡 24小时自动发货，每人限购1件，不支持7天无理由退款',
      detail_sold_count: 81,
      want_count: 46,
      browse_count: 1200,
    }),
  ]);
  assert.equal(evidence.confidence, 'high');
  assert.equal(evidence.benchmark.itemId, 'high-sales');
  assert.equal(evidence.benchmarkBasis, 'activity');
});

test('joint membership durations stay attached to their individual brands', () => {
  const upstream = { ...quote, title: '【账号直充】哔哩哔哩会员月卡+优酷会员日卡' };
  const swapped = listing({ title: '【账号直充】哔哩哔哩会员日卡+优酷会员月卡', detail_sold_count: 50 });
  const reordered = listing({ title: '【账号直充】优酷会员日卡+B站会员月卡', detail_sold_count: 10 });
  assert.equal(assessListing(upstream, swapped).comparable, false);
  assert.equal(assessListing(upstream, reordered).comparable, true);
});

test('verified wants provide a demand signal without qualifying as sales', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ want_count: 100, want_source: 'detail' }),
  ]);
  assert.equal(evidence.benchmark.itemId, '9001');
  assert.equal(evidence.eligibleForProfit, false);
  assert.match(evidence.reasons.join(' '), /仅有想要/);
});

test('search-page wants can rank listings but still cannot establish profit', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ itemId: 'search-want', price: 31, want_count: 1087, want_source: 'search' }),
  ]);
  assert.equal(evidence.benchmark.itemId, 'search-want');
  assert.equal(evidence.confidence, 'high');
  assert.equal(evidence.eligibleForProfit, false);
  assert.match(evidence.reasons.join(' '), /成交待核/);
});

test('a list-only sales count cannot replace a verified detail sale', () => {
  const evidence = selectMarketEvidence(quote, [
    listing({ sold_count: 40 }),
  ]);
  assert.equal(evidence.assessments[0].sold, null);
  assert.equal(evidence.eligibleForProfit, false);
});

test('old or failed snapshots remain visible and cannot establish a positive spread', () => {
  const now = Date.parse('2026-11-18T11:00:00+08:00');
  const stale = { items: [listing({ detail_sold_count: 6 })], fetchedAt: '2026-11-16T11:00:00+08:00' };
  const failed = { ...stale, fetchedAt: '2026-11-18T10:30:00+08:00', error: '模拟刷新失败' };
  for (const snapshot of [stale, failed]) {
    const result = selectObservedMarketEvidence(quote, [snapshot], false, now);
    assert.equal(result.evidence.benchmark?.itemId, '9001');
    assert.equal(result.evidence.eligibleForProfit, false);
  }
  assert.equal(isFreshSnapshot(null, 600_000, now), false);
  assert.equal(isFreshSnapshot('2026-11-18T10:40:00+08:00', 600_000, now), false);
});

test('the new listing and its timestamp stay together while earlier sales are excluded', () => {
  const now = Date.parse('2026-11-18T11:00:00+08:00');
  const previous = {
    items: [listing({ price: 31, detail_sold_count: 6 })],
    fetchedAt: '2026-11-18T10:20:00+08:00',
  };
  const newest = { items: [listing({ price: 28 })], fetchedAt: '2026-11-18T10:55:00+08:00' };
  const result = selectObservedMarketEvidence(quote, [newest, previous], false, now);
  assert.equal(result.evidence.benchmark?.price, 28);
  assert.equal(result.fetchedAt, newest.fetchedAt);
  assert.equal(result.evidence.eligibleForProfit, false);
  const priorFailedRefresh = selectObservedMarketEvidence(quote, [previous], true, now);
  assert.equal(priorFailedRefresh.evidence.eligibleForProfit, false);
});

test('the pre-fee listing margin uses the selling price and preserves missing costs', () => {
  const spread = calculatePriceSpread(1000, 1500);
  assert.equal(spread.gapCents, 500);
  assert.equal(spread.marginPercent.toFixed(1), '33.3');
  assert.equal(spread.netCents, null);
  assert.deepEqual(calculatePriceSpread(1000, 1500, 200), {
    gapCents: 500,
    marginPercent: 100 / 3,
    netCents: 300,
  });
});

test('the minimum for a SKU uses available fresh quotes before older lower prices', () => {
  const now = Date.parse('2026-11-18T10:00:00Z');
  const old = { ...quote, price: 10, fetched_at: '2026-11-18T09:58:00Z', stale: true };
  const fresh = { ...quote, price: 15, fetched_at: '2026-11-18T09:59:00Z' };
  const expired = { ...quote, price: 5, fetched_at: '2026-11-18T09:49:59Z' };
  assert.equal(selectLowestQuote(old, fresh, now), fresh);
  assert.equal(selectLowestQuote(fresh, expired, now), fresh);
  assert.equal(selectLowestQuote(old, expired, now), expired);
});
