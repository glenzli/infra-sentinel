#import "Localization.h"

static NSDictionary<NSString *, NSString *> *ChineseStrings(void) {
    static NSDictionary<NSString *, NSString *> *strings;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        strings = @{
            @"window.title": @"Traffic Sentinel 仪表板",
            @"button.reset": @"重置统计",
            @"button.install": @"安装 / 审核 Codex Hook",
            @"menu.open": @"打开仪表板",
            @"menu.restart": @"重新启动监控",
            @"menu.edit": @"编辑配置",
            @"menu.state": @"显示状态与日志",
            @"menu.install": @"安装 / 审核 Codex Hook",
            @"menu.language": @"Language / 语言",
            @"menu.chinese": @"中文",
            @"menu.english": @"English",
            @"menu.quit": @"退出",
            @"monitor.starting": @"正在启动",
            @"monitor.init_failed": @"初始化失败",
            @"monitor.running": @"运行中",
            @"monitor.restarting": @"正在重启",
            @"monitor.exit_format": @"监控已退出（代码 %d），10 秒后重试",
            @"monitor.launch_failed_format": @"无法启动监控：%@",
            @"status.sampling_failed": @"采样失败",
            @"status.starting": @"启动中",
            @"status.abnormal": @"监控异常",
            @"status.first_sample": @"首次启动需要等待一个采样周期。",
            @"status.local": @"本机",
            @"session.waiting": @"等待首次采样",
            @"session.menu_format": @"当前统计：%@",
            @"session.manual": @"手动重置",
            @"session.automatic": @"自动开始",
            @"session.header_format": @"当前统计：%@ · %@ · 已统计 %@",
            @"estimate.menu_format": @"估算上限 %.2f× · 其他设备逻辑流量约 %@",
            @"estimate.menu_waiting_format": @"估算上限 %.2f× · 等待 VPS 基线",
            @"alert.recent_format": @"最近告警：%@ / %@",
            @"notification.recovered_title": @"%@ 流量恢复",
            @"notification.deescalated_title": @"%@ 流量降级",
            @"notification.alert_title": @"%@ 流量告警",
            @"notification.critical_title": @"%@ 严重告警",
            @"notification.recovered_body": @"流量已回落到告警阈值以下。点击打开仪表板。",
            @"notification.deescalated_body": @"严重阈值已回落，仍处于警告范围。点击打开仪表板。",
            @"notification.critical_body": @"10 分钟累计 %@。点击打开仪表板。",
            @"notification.warning_body": @"5 分钟 ↑%@ ↓%@。点击打开仪表板。",
            @"dashboard.waiting_sample": @"等待首次采样",
            @"dashboard.updated_format": @"最近采样：%@",
            @"dashboard.vps_card": @"VPS 当前账单量",
            @"dashboard.vps_detail": @"入 %@  ·  出 %@",
            @"dashboard.proxy_card": @"本机代理外网",
            @"dashboard.proxy_detail": @"仅非回环接口",
            @"dashboard.ai_card": @"本机 AI 流量",
            @"dashboard.ai_detail": @"已配置项目合计",
            @"projects.title": @"项目流量",
            @"projects.waiting": @"尚无项目流量",
            @"estimate.title": @"估算拆分",
            @"estimate.ceiling_format": @"账单估算上限：%.0f ×（1 + %.0f%%）= %.2f×",
            @"estimate.local_other_format": @"本机其他流量约 %@",
            @"estimate.other_devices_format": @"其他设备：账单约 %@ → 逻辑流量约 %@",
            @"estimate.other_devices_waiting": @"其他设备：等待新的 VPS 计数区间",
            @"estimate.note": @"按配置上限保守估算；差额不会被判定为某个具体设备或应用。",
            @"trend.title_format": @"近 %ld 分钟速率趋势",
            @"trend.unit": @"单位：MiB/min",
            @"trend.proxy": @"代理外网",
            @"trend.waiting": @"统计刚开始，等待两个分钟数据点…",
            @"trend.ago_format": @"-%ld 分钟",
            @"trend.now": @"现在",
            @"notice.reset": @"当前统计已重置；本机从下一次采样开始，VPS 正在建立新的只读基线。",
            @"notice.reset_failed": @"无法重置当前统计：%@",
            @"notice.installing": @"正在安装并检查 Codex Hook…",
            @"notice.review_required": @"Hook 已安装，但仍待 Codex 信任审核。已打开官方审核界面；请选择“Trust all and continue”。",
            @"notice.trusted": @"Codex Hook 已安装并获得信任。重启 ChatGPT 后，旧任务或新任务都可记录后续事件。",
            @"notice.review_launch_failed": @"Hook 仍待审核，但无法打开官方审核界面：%@",
            @"notice.status_failed": @"Hook 已安装，但无法读取信任状态：%@",
            @"notice.install_failed": @"Codex 统计安装失败：%@",
            @"activity.title": @"Codex 活动与模型详情",
            @"activity.privacy": @"只记录事件计数与大小；不保存提示词、命令、路径或工具正文",
            @"activity.waiting": @"尚未收到 Codex 事件。点击“安装 / 审核 Codex Hook”，完成信任后重启 ChatGPT。",
            @"activity.last_event_format": @"事件已接入 · 最近 %@",
            @"activity.subagents_format": @"子 Agent：当前 %lld / 累计 %lld / 峰值 %lld",
            @"activity.tools_format": @"工具：%lld 次    读取类：%lld 次    重复候选：%lld 次    工具返回：%@",
            @"activity.model_header": @"模型",
            @"activity.traffic_header": @"模型流量",
            @"activity.quality_header": @"可信度",
            @"activity.activity_header": @"活动",
            @"activity.model_format": @"工具 %lld · 子 Agent %lld",
            @"activity.exclusive": @"高可信独占",
            @"activity.mixed_format": @"估算 · %.0f%% 独占",
            @"activity.no_traffic": @"等待流量",
            @"activity.unassigned_format": @"暂未分配到模型的 Codex 流量：%@",
            @"group.proxy": @"本地代理",
            @"group.other_monitored": @"其他项目",
            @"group.unnamed": @"未命名",
            @"duration.hours": @"%lld 小时 %lld 分",
            @"duration.minutes": @"%lld 分 %lld 秒",
            @"duration.seconds": @"%lld 秒",
            @"error.unknown": @"未知错误",
        };
    });
    return strings;
}

static NSDictionary<NSString *, NSString *> *EnglishStrings(void) {
    static NSDictionary<NSString *, NSString *> *strings;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        strings = @{
            @"window.title": @"Traffic Sentinel Dashboard",
            @"button.reset": @"Reset totals",
            @"button.install": @"Install / Review Codex Hook",
            @"menu.open": @"Open Dashboard",
            @"menu.restart": @"Restart Monitor",
            @"menu.edit": @"Edit Configuration",
            @"menu.state": @"Show State and Logs",
            @"menu.install": @"Install / Review Codex Hook",
            @"menu.language": @"Language / 语言",
            @"menu.chinese": @"中文",
            @"menu.english": @"English",
            @"menu.quit": @"Quit",
            @"monitor.starting": @"Starting",
            @"monitor.init_failed": @"Initialization failed",
            @"monitor.running": @"Running",
            @"monitor.restarting": @"Restarting",
            @"monitor.exit_format": @"Monitor exited (code %d); retrying in 10 seconds",
            @"monitor.launch_failed_format": @"Unable to start monitor: %@",
            @"status.sampling_failed": @"Sampling failed",
            @"status.starting": @"Starting",
            @"status.abnormal": @"Monitor error",
            @"status.first_sample": @"The first sample takes one sampling interval.",
            @"status.local": @"Local",
            @"session.waiting": @"Waiting for first sample",
            @"session.menu_format": @"Current totals: %@",
            @"session.manual": @"manual reset",
            @"session.automatic": @"automatic start",
            @"session.header_format": @"Current totals: %@ · %@ · tracking for %@",
            @"estimate.menu_format": @"Ceiling %.2f× · other-device logical traffic ≈ %@",
            @"estimate.menu_waiting_format": @"Ceiling %.2f× · waiting for VPS baseline",
            @"alert.recent_format": @"Latest alert: %@ / %@",
            @"notification.recovered_title": @"%@ traffic recovered",
            @"notification.deescalated_title": @"%@ traffic de-escalated",
            @"notification.alert_title": @"%@ traffic alert",
            @"notification.critical_title": @"%@ critical traffic alert",
            @"notification.recovered_body": @"Traffic is below the alert threshold. Click to open the dashboard.",
            @"notification.deescalated_body": @"The critical threshold cleared; warning traffic remains. Click to open the dashboard.",
            @"notification.critical_body": @"10-minute total: %@. Click to open the dashboard.",
            @"notification.warning_body": @"5 minutes ↑%@ ↓%@. Click to open the dashboard.",
            @"dashboard.waiting_sample": @"Waiting for first sample",
            @"dashboard.updated_format": @"Latest sample: %@",
            @"dashboard.vps_card": @"Current VPS billing",
            @"dashboard.vps_detail": @"In %@  ·  Out %@",
            @"dashboard.proxy_card": @"Local proxy external",
            @"dashboard.proxy_detail": @"Non-loopback interfaces only",
            @"dashboard.ai_card": @"Local AI traffic",
            @"dashboard.ai_detail": @"Configured projects combined",
            @"projects.title": @"Project traffic",
            @"projects.waiting": @"No project traffic yet",
            @"estimate.title": @"Estimated split",
            @"estimate.ceiling_format": @"Billing ceiling: %.0f × (1 + %.0f%%) = %.2f×",
            @"estimate.local_other_format": @"Other local traffic ≈ %@",
            @"estimate.other_devices_format": @"Other devices: billable ≈ %@ → logical ≈ %@",
            @"estimate.other_devices_waiting": @"Other devices: waiting for a new VPS interval",
            @"estimate.note": @"Conservative configured ceiling; no remainder is assigned to a specific device or app.",
            @"trend.title_format": @"Last %ld minutes — rate",
            @"trend.unit": @"Unit: MiB/min",
            @"trend.proxy": @"Proxy external",
            @"trend.waiting": @"Tracking just started; waiting for two minute points…",
            @"trend.ago_format": @"-%ld min",
            @"trend.now": @"Now",
            @"notice.reset": @"Current totals reset. Local tracking resumes on the next sample; VPS is taking a new read-only baseline.",
            @"notice.reset_failed": @"Unable to reset current totals: %@",
            @"notice.installing": @"Installing and checking the Codex Hook…",
            @"notice.review_required": @"The Hook is installed but still needs Codex trust review. The official review screen is open; choose “Trust all and continue.”",
            @"notice.trusted": @"The Codex Hook is installed and trusted. Restart ChatGPT; old or new tasks can then record subsequent events.",
            @"notice.review_launch_failed": @"The Hook still needs review, but the official review screen could not be opened: %@",
            @"notice.status_failed": @"The Hook is installed, but its trust status could not be read: %@",
            @"notice.install_failed": @"Codex statistics installation failed: %@",
            @"activity.title": @"Codex activity and model details",
            @"activity.privacy": @"Stores event counts and sizes only; no prompts, commands, paths, or tool contents",
            @"activity.waiting": @"No Codex events yet. Click “Install / Review Codex Hook,” complete trust review, then restart ChatGPT.",
            @"activity.last_event_format": @"Events connected · latest %@",
            @"activity.subagents_format": @"Subagents: %lld active / %lld total / %lld peak",
            @"activity.tools_format": @"Tools: %lld    Read-like: %lld    Repeat candidates: %lld    Tool output: %@",
            @"activity.model_header": @"Model",
            @"activity.traffic_header": @"Model traffic",
            @"activity.quality_header": @"Confidence",
            @"activity.activity_header": @"Activity",
            @"activity.model_format": @"%lld tools · %lld subagents",
            @"activity.exclusive": @"high-confidence exclusive",
            @"activity.mixed_format": @"estimated · %.0f%% exclusive",
            @"activity.no_traffic": @"waiting for traffic",
            @"activity.unassigned_format": @"Codex traffic not yet assigned to a model: %@",
            @"group.proxy": @"Local proxy",
            @"group.other_monitored": @"Other projects",
            @"group.unnamed": @"Unnamed",
            @"duration.hours": @"%lld h %lld min",
            @"duration.minutes": @"%lld min %lld sec",
            @"duration.seconds": @"%lld sec",
            @"error.unknown": @"unknown error",
        };
    });
    return strings;
}

TSLanguage TSDefaultLanguage(void) {
    NSString *preferred = [NSLocale preferredLanguages].firstObject.lowercaseString ?: @"en";
    return [preferred hasPrefix:@"zh"] ? TSLanguageChinese : TSLanguageEnglish;
}

NSString *TSLanguageIdentifier(TSLanguage language) {
    return language == TSLanguageEnglish ? @"en" : @"zh-Hans";
}

TSLanguage TSLanguageFromIdentifier(NSString *identifier) {
    if ([identifier.lowercaseString hasPrefix:@"en"]) {
        return TSLanguageEnglish;
    }
    if ([identifier.lowercaseString hasPrefix:@"zh"]) {
        return TSLanguageChinese;
    }
    return TSDefaultLanguage();
}

NSString *TSLocalized(TSLanguage language, NSString *key) {
    NSDictionary *table = language == TSLanguageEnglish ? EnglishStrings() : ChineseStrings();
    return table[key] ?: EnglishStrings()[key] ?: key;
}

NSString *TSLocalizedGroupLabel(TSLanguage language, NSDictionary *group) {
    NSString *groupID = [group[@"id"] isKindOfClass:[NSString class]] ? group[@"id"] : @"";
    if ([groupID isEqualToString:@"proxy"]) {
        return TSLocalized(language, @"group.proxy");
    }
    if ([groupID isEqualToString:@"other_monitored"]) {
        return TSLocalized(language, @"group.other_monitored");
    }
    NSString *label = [group[@"label"] isKindOfClass:[NSString class]] ? group[@"label"] : @"";
    return label.length > 0 ? label : TSLocalized(language, @"group.unnamed");
}
