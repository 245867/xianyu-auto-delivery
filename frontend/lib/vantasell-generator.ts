/**
 * VantaSell 风格五段式结构化商品文案生成与静态合规体检引擎
 * 针对闲鱼虚拟/卡券/数字商品场景深度定制，契合 publish_guard 规范
 */

export interface ComplianceIssue {
  word: string;
  level: 'block' | 'warn';
  replacement: string;
  reason: string;
}

export interface ComplianceAuditResult {
  passed: boolean;
  blockCount: number;
  warnCount: number;
  issues: ComplianceIssue[];
}

// 严禁导流/站外交易词（命中即拒绝）
const BLOCK_PATTERNS: Array<{ regex: RegExp; word: string; replacement: string; reason: string }> = [
  { regex: /微信|微信号|加微|加v|vx|v信/gi, word: '微信/加V', replacement: '平台私信', reason: '严禁站外导流 (E07违规)' },
  { regex: /qq|扣扣|企鹅群|qq群/gi, word: 'QQ/QQ群', replacement: '闲鱼消息', reason: '严禁站外导流 (E07违规)' },
  { regex: /二维码|扫码付款|扫码加群/gi, word: '二维码/扫码', replacement: '在线卡密', reason: '严禁站外扫码导流' },
  { regex: /淘宝|拼多多|京东|转转|快手|抖音/gi, word: '竞品电商名', replacement: '官方正规', reason: '站外平台导流风险' },
  { regex: /代充|慢充/gi, word: '代充/慢充', replacement: '卡密兑换码', reason: '充值资金合规高危词' },
  { regex: /刷单|兼职|返利/gi, word: '刷单/兼职', replacement: '正规权益', reason: '高危风控拦截词' },
];

// 极限词 / 夸大虚假宣传词（提示并建议修改）
const WARN_PATTERNS: Array<{ regex: RegExp; word: string; replacement: string; reason: string }> = [
  { regex: /100%|百分之百/g, word: '100%', replacement: '高标准', reason: '极限词易被平台降权' },
  { regex: /最好|第一|全网第一|极其|顶尖/g, word: '最好/第一', replacement: '优质精选', reason: '广告法极限用语' },
  { regex: /绝对|保证正品|假一赔十/g, word: '绝对/保证正品', replacement: '官方支持验券', reason: '二手平台售假判定高危词' },
  { regex: /永久|终身|终生/g, word: '永久/终身', replacement: '稳定长期可用', reason: '虚拟商品无法兑现承诺' },
  { regex: /暴富|暴赚|日赚|躺赚/g, word: '暴富/暴赚', replacement: '实用提效', reason: '违规吸睛与夸大宣传' },
  { regex: /破解|绿色破解|汉化去弹窗/g, word: '破解', replacement: '免配置精编版', reason: '软件版权侵权风险' },
];

/**
 * 生成标准的五段式结构化宝贝描述 (VantaSell 积木块标准)
 */
export function generateFiveBlockDesc(params: {
  title: string;
  supplier?: string;
  price?: string;
  category?: string;
}): string {
  const { title, supplier, price } = params;
  const cleanTitle = title.trim();

  return [
    `【刚需亮点】`,
    `🔥 官方渠道正规权益，即拍即发！省去繁琐比价，稳定靠谱，省时省心。`,
    ``,
    `【商品明细与规格】`,
    `📦 商品名称：${cleanTitle}`,
    `💎 货源说明：${supplier ? `正规 ${supplier} 权益源，` : ''}正品凭证支持核验，无虚标时长/面额。`,
    price ? `💰 现行优选特惠：¥${price}，薄利多销，拍下即享。` : `💰 拍下即可查看对应规格明细。`,
    ``,
    `【自动履约流程】`,
    `⚡ 智控系统已接入 24 小时无人值守全自动发货服务。`,
    `1. 拍下并完成付款后，系统将在 1~3 秒内自动通过闲鱼聊天窗口发送卡密/使用信息。`,
    `2. 深夜、清晨拍下同样秒发，无需等待店主在线，随时拍随时到账。`,
    ``,
    `【简易使用说明】`,
    `📱 收到卡密/信息后，按照发送的简要指引前往对应官方 App 或指定入口兑换即可。`,
    `🔑 建议收到后尽快核销兑换，保障您的使用权益。`,
    ``,
    `【购买须知与售后保障】`,
    `🛡️ 虚拟数字商品具有时效与可复制性，卡密一经系统发出概不受理非质量问题的退换哦。`,
    `🤝 质保期内如遇任何兑换疑问或异常，请随时在闲鱼聊天窗口联系客服，看到后必第一时间为您妥善排查解决，感谢支持！`,
  ].join('\n');
}

/**
 * 对文案进行静态违规体检
 */
export function auditContent(title: string, desc: string): ComplianceAuditResult {
  const fullText = `${title}\n${desc}`;
  const issues: ComplianceIssue[] = [];

  // 检测 Block 词
  for (const item of BLOCK_PATTERNS) {
    if (item.regex.test(fullText)) {
      issues.push({
        word: item.word,
        level: 'block',
        replacement: item.replacement,
        reason: item.reason,
      });
    }
  }

  // 检测 Warn 词
  for (const item of WARN_PATTERNS) {
    if (item.regex.test(fullText)) {
      issues.push({
        word: item.word,
        level: 'warn',
        replacement: item.replacement,
        reason: item.reason,
      });
    }
  }

  const blockCount = issues.filter(i => i.level === 'block').length;
  const warnCount = issues.filter(i => i.level === 'warn').length;

  return {
    passed: blockCount === 0,
    blockCount,
    warnCount,
    issues,
  };
}

/**
 * 一键安全净化文案：将发现的所有违禁词/极限词替换为安全替代词
 */
export function sanitizeContent(text: string): string {
  let sanitized = text;

  for (const item of BLOCK_PATTERNS) {
    sanitized = sanitized.replace(item.regex, item.replacement);
  }

  for (const item of WARN_PATTERNS) {
    sanitized = sanitized.replace(item.regex, item.replacement);
  }

  return sanitized;
}
