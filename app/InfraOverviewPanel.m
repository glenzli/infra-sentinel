#import "InfraOverviewPanel.h"
#import "MonitorHealth.h"
#import "TrafficFormatting.h"

static NSDictionary *InfraDictionary(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

static NSArray *InfraArray(id value) {
    return [value isKindOfClass:[NSArray class]] ? value : @[];
}

static void DrawInfraText(NSString *text, NSRect rect, NSFont *font, NSColor *color, NSLineBreakMode mode) {
    NSMutableParagraphStyle *style = [[NSMutableParagraphStyle alloc] init];
    style.lineBreakMode = mode;
    [text drawWithRect:rect
               options:(NSStringDrawingUsesLineFragmentOrigin | NSStringDrawingTruncatesLastVisibleLine)
            attributes:@{
                NSFontAttributeName: font,
                NSForegroundColorAttributeName: color,
                NSParagraphStyleAttributeName: style,
            }];
}

static NSString *InfraStatusKey(NSString *status) {
    if ([status isEqualToString:@"critical"]) return @"infra.status.critical";
    if ([status isEqualToString:@"warning"]) return @"infra.status.warning";
    if ([status isEqualToString:@"degraded"]) return @"infra.status.degraded";
    return @"infra.status.healthy";
}

static NSColor *InfraStatusColor(NSString *status) {
    if ([status isEqualToString:@"critical"]) return [NSColor systemRedColor];
    if ([status isEqualToString:@"warning"]) return [NSColor systemOrangeColor];
    if ([status isEqualToString:@"degraded"]) return [NSColor systemRedColor];
    return [NSColor systemGreenColor];
}

void TSDrawInfraOverviewPanel(NSRect rect, NSDictionary *infra, NSDictionary *health, TSLanguage language) {
    NSDictionary *overall = InfraDictionary(infra[@"overall"]);
    NSDictionary *network = @{};
    for (NSDictionary *resource in InfraArray(infra[@"resources"])) {
        if ([resource isKindOfClass:[NSDictionary class]] && [resource[@"id"] isEqualToString:@"network"]) {
            network = resource;
            break;
        }
    }
    NSString *status = TSMonitorHealthHasError(health) ? @"degraded" : (overall[@"status"] ?: @"healthy");
    if (![status isKindOfClass:[NSString class]]) status = @"healthy";
    NSColor *accent = InfraStatusColor(status);
    [[NSColor controlBackgroundColor] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:rect xRadius:12 yRadius:12] fill];
    [accent setFill];
    [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(rect.origin.x, rect.origin.y, 5, rect.size.height)
                                      xRadius:2.5 yRadius:2.5] fill];

    CGFloat inset = 18;
    CGFloat dividerX = rect.origin.x + rect.size.width * 0.38;
    DrawInfraText(TSLocalized(language, @"infra.overview.title"),
                  NSMakeRect(rect.origin.x + inset, NSMaxY(rect) - 29, dividerX - rect.origin.x - inset * 2, 17),
                  [NSFont systemFontOfSize:13 weight:NSFontWeightMedium], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
    DrawInfraText(TSLocalized(language, InfraStatusKey(status)),
                  NSMakeRect(rect.origin.x + inset, rect.origin.y + 42, dividerX - rect.origin.x - inset * 2, 27),
                  [NSFont systemFontOfSize:21 weight:NSFontWeightSemibold], accent, NSLineBreakByTruncatingTail);
    NSInteger activeAlerts = [overall[@"active_alerts"] integerValue];
    NSString *healthDetail = activeAlerts > 0
        ? [NSString stringWithFormat:TSLocalized(language, @"infra.overview.alert_count"), activeAlerts]
        : TSLocalized(language, @"infra.overview.no_alerts");
    DrawInfraText(healthDetail, NSMakeRect(rect.origin.x + inset, rect.origin.y + 19, dividerX - rect.origin.x - inset * 2, 16),
                  [NSFont systemFontOfSize:11], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);

    [[NSColor separatorColor] setStroke];
    NSBezierPath *divider = [NSBezierPath bezierPath];
    [divider moveToPoint:NSMakePoint(dividerX, rect.origin.y + 16)];
    [divider lineToPoint:NSMakePoint(dividerX, NSMaxY(rect) - 16)];
    divider.lineWidth = 0.5;
    [divider stroke];

    CGFloat networkX = dividerX + inset;
    DrawInfraText(TSLocalized(language, @"resource.network"), NSMakeRect(networkX, NSMaxY(rect) - 29, 180, 17),
                  [NSFont systemFontOfSize:13 weight:NSFontWeightMedium], [NSColor labelColor], NSLineBreakByTruncatingTail);
    DrawInfraText(TSFormatBytes([network[@"primary_value"] longLongValue]), NSMakeRect(networkX, rect.origin.y + 41, 170, 28),
                  [NSFont monospacedDigitSystemFontOfSize:21 weight:NSFontWeightSemibold], [NSColor labelColor], NSLineBreakByTruncatingTail);
    NSString *sourceDetail = [NSString stringWithFormat:TSLocalized(language, @"infra.network.sources_format"),
                              [network[@"online_source_count"] integerValue], [network[@"source_count"] integerValue]];
    DrawInfraText(sourceDetail, NSMakeRect(networkX, rect.origin.y + 19, 205, 16),
                  [NSFont systemFontOfSize:11], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);

    CGFloat sourcesX = rect.origin.x + rect.size.width * 0.72;
    NSArray *sources = InfraArray(infra[@"sources"]);
    NSInteger configured = 0;
    for (NSDictionary *source in sources) if ([source[@"enabled"] boolValue]) configured++;
    DrawInfraText(TSLocalized(language, @"infra.sources.title"), NSMakeRect(sourcesX, NSMaxY(rect) - 29, rect.size.width * 0.25 - inset, 17),
                  [NSFont systemFontOfSize:13 weight:NSFontWeightMedium], [NSColor labelColor], NSLineBreakByTruncatingTail);
    DrawInfraText([NSString stringWithFormat:TSLocalized(language, @"infra.sources.configured_format"), configured],
                  NSMakeRect(sourcesX, rect.origin.y + 45, rect.size.width * 0.25 - inset, 20),
                  [NSFont systemFontOfSize:16 weight:NSFontWeightSemibold], [NSColor labelColor], NSLineBreakByTruncatingTail);
    DrawInfraText(TSLocalized(language, @"infra.sources.network_only"),
                  NSMakeRect(sourcesX, rect.origin.y + 20, rect.size.width * 0.25 - inset, 16),
                  [NSFont systemFontOfSize:11], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
}
