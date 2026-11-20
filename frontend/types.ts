
// API Response Bases
export interface ApiResponse {
  success?: boolean;
  message?: string;
  msg?: string;
}

export interface PaginatedResponse<T> {
  success: boolean;
  data: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

// Auth
export interface LoginResponse {
  success: boolean;
  token?: string;
  message?: string;
  user_id?: number;
  username?: string;
  is_admin?: boolean;
}

// Accounts
export type AccountConnectionState = 'disconnected' | 'connecting' | 'connected' | 'reconnecting' | 'failed' | 'closed';

export interface AccountDetail {
  id: string;
  value?: string; // cookie value from backend
  cookie?: string; // alias for value
  enabled: boolean;
  connection_state?: AccountConnectionState | null;
  auto_confirm: boolean;
  remark?: string;
  note?: string; // alias for remark
  pause_duration?: number;
  // 登录信息
  username?: string;
  login_password?: string;
  show_browser?: boolean;
  // Frontend helpers
  nickname?: string;
  avatar_url?: string;
  // AI设置
  ai_enabled?: boolean;
  max_discount_percent?: number;
  max_discount_amount?: number;
  max_bargain_rounds?: number;
  custom_prompts?: string;
}

// Orders
export type OrderStatus = 
  | 'processing'      
  | 'pending_ship'    
  | 'shipped'         
  | 'completed'       
  | 'cancelled'       
  | 'refunding';

export interface Order {
  id: string;
  order_id: string;
  cookie_id: string;
  item_id: string;
  item_title?: string;
  item_image?: string;
  item_price?: string;
  buyer_id: string;
  quantity: number;
  amount: string;
  status: OrderStatus;
  receiver_name?: string;
  receiver_phone?: string;
  receiver_address?: string;
  created_at?: string;
  updated_at?: string;
}

// Cards
export interface Card {
  id: number;
  name: string;
  type: 'api' | 'text' | 'data' | 'image';
  description?: string;
  enabled: boolean;
  // 文本类型
  text_content?: string;
  // 批量数据类型
  data_content?: string;
  // API 类型配置
  api_config?: {
    url: string;
    method: 'GET' | 'POST';
    timeout?: number;
    headers?: string;
    params?: string;
  };
  // 图片类型
  image_url?: string;
  // 通用配置
  delay_seconds?: number;
  after_delivery_enabled?: boolean;
  after_delivery_message?: string;
  // 多规格配置
  is_multi_spec?: boolean;
  spec_name?: string;
  spec_value?: string;
  spec_name_2?: string;
  spec_value_2?: string;
  created_at: string;
  updated_at: string;
}

// Items
export interface Item {
  id: string | number;
  cookie_id: string;
  item_id: string;
  item_title?: string;
  item_price?: string;
  item_image?: string; // Inferred from common usage, though not explicitly in list model sometimes
  item_category?: string;
  is_multi_spec?: number | boolean;
  multi_quantity_delivery?: number | boolean;
  ai_reply_enabled?: boolean | null;
  auto_delivery_enabled?: number | boolean;
  delivery_card_id?: number | null;
  refund_blacklist_enabled?: number | boolean;
  created_at?: string;
}

export interface RefundBlacklistEntry {
  id: number;
  buyer_id: string;
  cookie_id: string;
  first_order_id?: string | null;
  first_item_id?: string | null;
  last_order_id?: string | null;
  last_item_id?: string | null;
  reason: string;
  note: string;
  refund_count: number;
  enabled: number | boolean;
  is_whitelisted: number | boolean;
  source: 'refund' | 'manual' | string;
  created_at: string;
  updated_at: string;
}

export interface RefundBlacklistPayload {
  buyer_id: string;
  cookie_id: string;
  order_id?: string;
  item_id?: string;
  reason?: string;
  note?: string;
  enabled?: boolean;
  is_whitelisted?: boolean;
}

// Rules
export interface ShippingRule {
  id: string;
  name: string;
  item_keyword: string; // Matches item title
  card_group_id: number; // ID from Card list
  card_group_name?: string; // UI helper
  priority: number;
  enabled: boolean;
}

export interface ReplyRule {
  id: string;
  keyword: string;
  reply_content: string;
  match_type: 'exact' | 'fuzzy';
  enabled: boolean;
}

// Stats
export interface AdminStats {
  total_users: number;
  total_cookies: number;
  active_cookies: number;
  total_cards: number;
  available_cards: number;
  total_keywords: number;
  total_orders: number;
}

export interface OrderAnalytics {
  revenue_stats: {
    total_amount: number;
    total_orders: number;
  };
  daily_stats: Array<{ date: string; amount: number; order_count: number }>;
  item_stats?: Array<{
    item_id: string;
    order_count: number;
    total_amount: number;
    avg_amount: number;
  }>;
}

// Settings
export interface SystemSettings {
  default_reply?: string;
  registration_enabled?: boolean;
  show_default_login_info?: boolean;
  login_captcha_enabled?: boolean;
  item_sync_enabled?: boolean;
  item_sync_interval?: number;
  item_sync_max_pages?: number;
  refund_blacklist_global_enabled?: boolean;
  smtp_server?: string;
  [key: string]: any;
}

export interface AIReplySettings {
  ai_enabled: boolean;
  model_name?: string;
  wire_api?: string;
  provider_configured?: boolean;
  max_discount_percent: number;
  max_discount_amount?: number;
  max_bargain_rounds: number;
  custom_prompts: string;
}

// Default Reply
export interface DefaultReply {
  cookie_id: string;
  enabled: boolean;
  reply_content: string;
  reply_once: boolean;
  reply_image_url?: string;
}

// Opportunity fulfillment
export type OpportunityFulfillmentDelivery =
  | '无需邮寄'
  | '包邮'
  | '按距离计费'
  | '一口价';

export interface OpportunityFulfillmentSource {
  title: string;
  price: number;
  supplier: string;
  url: string;
  source?: string;
  source_label?: string;
  source_url?: string;
  updated_at?: string | null;
}

export interface OpportunityFulfillmentCategory {
  catId: string;
  catName: string;
  channelCatId: string;
  tbCatId: string;
}

export interface OpportunityFulfillmentTaskPayload {
  cookie_id: string;
  card_id: number;
  title: string;
  desc: string;
  imageUrls: string[];
  price: number;
  quantity: number;
  delivery: OpportunityFulfillmentDelivery;
  postPrice: number;
  tags?: string[];
  category?: OpportunityFulfillmentCategory;
  source?: OpportunityFulfillmentSource;
}

export type OpportunityFulfillmentStatus =
  | 'draft'
  | 'ready_to_publish'
  | 'inventory_not_ready'
  | 'publishing'
  | 'publish_throttled'
  | 'publish_blocked_night'
  | 'publish_blocked_guard'
  | 'submitted_pending_readback'
  | 'readback_not_visible'
  | 'readback_failed'
  | 'readback_confirmed'
  | 'sync_confirmed'
  | 'delivery_bound'
  | 'delivery_ready'
  | 'submission_unknown_review_required'
  | 'duplicate_submission'
  | 'failed';

export interface OpportunityFulfillmentInventory {
  ready: boolean;
  kind: string;
  available?: number;
  reason: string;
}

export interface OpportunityFulfillmentTaskResult {
  inventory?: OpportunityFulfillmentInventory;
  guard_warnings?: string[];
  supplier_procurement?: {
    configured: boolean;
    mode: string;
    message: string;
  };
}

export interface OpportunityFulfillmentTask {
  id: string;
  user_id: number;
  cookie_id: string;
  card_id: number;
  status: OpportunityFulfillmentStatus;
  stage: string;
  message: string;
  idempotency_key: string;
  item_id: string;
  payload: OpportunityFulfillmentTaskPayload;
  result: OpportunityFulfillmentTaskResult;
  item_detail: Record<string, unknown>;
  error: string;
  created_at: string;
  updated_at: string;
}

export interface OpportunityFulfillmentEvent {
  id: number;
  status: OpportunityFulfillmentStatus;
  stage: string;
  message: string;
  detail: Record<string, unknown>;
  created_at: string;
}

export interface OpportunityFulfillmentCreateResponse {
  ok: boolean;
  task: OpportunityFulfillmentTask;
}

export interface OpportunityFulfillmentTaskResponse {
  ok: boolean;
  task: OpportunityFulfillmentTask;
  events: OpportunityFulfillmentEvent[];
}

export interface OpportunityFulfillmentRecoverResponse {
  ok: boolean;
  task: OpportunityFulfillmentTask;
  scheduled: boolean;
}

// Materials
export interface MaterialItem {
  id: number;
  name: string;
  kind: 'image' | 'text';
  tags: string[];
  tags_text: string;
  note: string;
  content: string;
  file_count: number;
  created_at: string;
  updated_at: string;
  cover_url: string;
}

export interface MaterialTagCount {
  tag: string;
  count: number;
}

export interface MaterialStats {
  materials: number;
  files: number;
  bytes: number;
  bytes_text: string;
  max_files: number;
}

export interface MaterialListResponse {
  ok: boolean;
  items: MaterialItem[];
  total: number;
  tags: MaterialTagCount[];
  stats: MaterialStats;
  error?: string;
}

export interface MaterialPathsResponse {
  ok: boolean;
  id?: number;
  name?: string;
  paths?: string[];
  count?: number;
  error?: string;
}

// 智能配图生图
export interface GenerateImagePayload {
  title: string;
  price?: number;
  tags?: string[];
  mode?: 'auto' | 'canvas' | 'ai';
  count?: number;
}

export interface GeneratedImageItem {
  path: string;
  url: string;
  filename: string;
  mode_used: 'ai' | 'canvas';
  kind: 'main' | 'guide' | 'guarantee';
}

export interface GenerateImageResponse {
  ok: boolean;
  images?: GeneratedImageItem[];
  paths?: string[];
  urls?: string[];
  primary_path?: string;
  primary_url?: string;
  error?: string;
  message?: string;
}
