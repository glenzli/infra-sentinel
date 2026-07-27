#import "Localization.h"

static NSDictionary<NSString *, NSString *> *ChineseStrings(void) {
    static NSDictionary<NSString *, NSString *> *strings;
    static dispatch_once_t onceToken;
    dispatch_once(&onceToken, ^{
        strings = @{
            @"window.title": @"Traffic Sentinel 仪表板",
            @"button.reset": @"重置统计",
            @"menu.open": @"打开仪表板",
            @"menu.restart": @"重新启动监控",
            @"menu.edit": @"编辑配置",
            @"menu.state": @"显示状态与日志",
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
            @"status.first_sample": @"正在连接 Mihomo 本机接口并建立累计基线。",
            @"status.local": @"本机",
            @"session.waiting": @"等待首次采样",
            @"session.menu_format": @"当前统计：%@",
            @"session.manual": @"手动重置",
            @"session.automatic": @"自动开始",
            @"session.header_format": @"当前统计：%@ · %@ · 已统计 %@",
            @"estimate.menu_format": @"实测 %.2f× · 包装占账单 %.1f%%",
            @"estimate.menu_waiting_format": @"实测倍率：等待 VPS / Xray 对齐",
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
            @"dashboard.mihomo_card": @"Mihomo 本机总量",
            @"dashboard.mihomo_detail": @"↑ %@  ·  ↓ %@ · 精确累计",
            @"dashboard.proxy_card": @"已识别代理路径",
            @"dashboard.proxy_detail": @"域名连接下限 · 未归因 %@",
            @"services.title": @"域名流量归因",
            @"services.waiting": @"等待出现可归因的域名流量",
            @"attribution.coverage_format": @"归因覆盖 %.1f%% · 未归因 %@ · DIRECT %@",
            @"xray.title": @"VPS 用户逻辑流量",
            @"xray.total_format": @"合计 %@",
            @"xray.directions_format": @"↑ %@ · ↓ %@",
            @"xray.other_users_format": @"其他 %ld 个身份",
            @"xray.no_users": @"尚未发现用户统计",
            @"xray.disabled": @"未启用 Xray 用户统计",
            @"xray.waiting": @"基线已建立，等待下一个完整采样区间",
            @"xray.updated_format": @"低频只读采样 · 最近 %@",
            @"xray.error_format": @"Xray 统计读取失败：%@",
            @"estimate.empirical_format": @"实测账单倍率 %.2f× · 双边基准 %.2f× · 账单附加率 +%.1f%%",
            @"estimate.empirical_waiting": @"等待 VPS 与 Xray 的完整对齐区间",
            @"estimate.packaging_format": @"包装占账单 %.1f%% · 小包 / ACK 约 %.1f%% · 连接 / 填充等约 %.1f%% · 平均包 %.0f B",
            @"estimate.packet_waiting_format": @"包装占账单 %.1f%% · 等待下一个 VPS 包数区间以拆分来源",
            @"estimate.fixed_disabled": @"固定 20% 估算已停用；完整区间产生后显示实测值",
            @"trend.title_format": @"近 %ld 分钟速率趋势",
            @"trend.unit": @"单位：MiB/min",
            @"trend.mihomo": @"Mihomo 总量",
            @"trend.waiting": @"统计刚开始，等待两个分钟数据点…",
            @"trend.ago_format": @"-%ld 分钟",
            @"trend.now": @"现在",
            @"reset.confirm_title": @"重置当前统计？",
            @"reset.confirm_message": @"这会清除当前统计周期内的 Mihomo、VPS 与 Xray 累计，并从下一采样区间重新建立基线。此操作无法撤销。",
            @"reset.confirm_action": @"重置",
            @"reset.cancel_action": @"取消",
            @"notice.reset": @"当前统计已重置；Mihomo 从下一采样区间累计，VPS 与 Xray 正在建立新的只读基线。",
            @"notice.reset_failed": @"无法重置当前统计：%@",
            @"group.proxy": @"本地代理",
            @"group.other_monitored": @"其他项目",
            @"group.other_domains": @"其他域名",
            @"group.unattributed": @"未归因",
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
            @"menu.open": @"Open Dashboard",
            @"menu.restart": @"Restart Monitor",
            @"menu.edit": @"Edit Configuration",
            @"menu.state": @"Show State and Logs",
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
            @"status.first_sample": @"Connecting to the local Mihomo interface and establishing a baseline.",
            @"status.local": @"Local",
            @"session.waiting": @"Waiting for first sample",
            @"session.menu_format": @"Current totals: %@",
            @"session.manual": @"manual reset",
            @"session.automatic": @"automatic start",
            @"session.header_format": @"Current totals: %@ · %@ · tracking for %@",
            @"estimate.menu_format": @"Measured %.2f× · packaging %.1f%% of bill",
            @"estimate.menu_waiting_format": @"Measured factor: waiting for VPS / Xray alignment",
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
            @"dashboard.mihomo_card": @"Local Mihomo total",
            @"dashboard.mihomo_detail": @"↑ %@  ·  ↓ %@ · exact cumulative",
            @"dashboard.proxy_card": @"Observed proxy route",
            @"dashboard.proxy_detail": @"domain connection floor · %@ unattributed",
            @"services.title": @"Domain traffic attribution",
            @"services.waiting": @"Waiting for attributable domain traffic",
            @"attribution.coverage_format": @"%.1f%% covered · %@ unattributed · %@ DIRECT",
            @"xray.title": @"VPS user logical traffic",
            @"xray.total_format": @"Total %@",
            @"xray.directions_format": @"↑ %@ · ↓ %@",
            @"xray.other_users_format": @"%ld other identities",
            @"xray.no_users": @"No user statistics discovered yet",
            @"xray.disabled": @"Xray user statistics disabled",
            @"xray.waiting": @"Baseline ready; waiting for one complete interval",
            @"xray.updated_format": @"Low-frequency read-only sample · latest %@",
            @"xray.error_format": @"Unable to read Xray statistics: %@",
            @"estimate.empirical_format": @"Measured billing %.2f× · two-leg baseline %.2f× · bill uplift +%.1f%%",
            @"estimate.empirical_waiting": @"Waiting for a complete aligned VPS and Xray interval",
            @"estimate.packaging_format": @"Packaging %.1f%% of bill · packets / ACKs ≈ %.1f%% · connections / padding ≈ %.1f%% · avg packet %.0f B",
            @"estimate.packet_waiting_format": @"Packaging %.1f%% of bill · waiting for the next VPS packet interval to split sources",
            @"estimate.fixed_disabled": @"The fixed 20% estimate is off; measured values appear after a complete interval",
            @"trend.title_format": @"Last %ld minutes — rate",
            @"trend.unit": @"Unit: MiB/min",
            @"trend.mihomo": @"Mihomo total",
            @"trend.waiting": @"Tracking just started; waiting for two minute points…",
            @"trend.ago_format": @"-%ld min",
            @"trend.now": @"Now",
            @"reset.confirm_title": @"Reset current totals?",
            @"reset.confirm_message": @"This clears the current Mihomo, VPS, and Xray totals and establishes new baselines from the next sample interval. This action cannot be undone.",
            @"reset.confirm_action": @"Reset",
            @"reset.cancel_action": @"Cancel",
            @"notice.reset": @"Current totals reset. Mihomo resumes with the next interval; VPS and Xray are taking new read-only baselines.",
            @"notice.reset_failed": @"Unable to reset current totals: %@",
            @"group.proxy": @"Local proxy",
            @"group.other_monitored": @"Other projects",
            @"group.other_domains": @"Other domains",
            @"group.unattributed": @"Unattributed",
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
    if ([groupID isEqualToString:@"other_domains"]) {
        return TSLocalized(language, @"group.other_domains");
    }
    if ([groupID isEqualToString:@"unattributed"]) {
        return TSLocalized(language, @"group.unattributed");
    }
    NSString *label = [group[@"label"] isKindOfClass:[NSString class]] ? group[@"label"] : @"";
    return label.length > 0 ? label : TSLocalized(language, @"group.unnamed");
}
