#import "TrafficOverviewPanel.h"
#import "TrafficFormatting.h"
#import "XrayUserPanel.h"

static NSDictionary *OverviewDictionary(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

static NSArray *OverviewArray(id value) {
    return [value isKindOfClass:[NSArray class]] ? value : @[];
}

static long long OverviewNumber(id value) {
    return [value respondsToSelector:@selector(longLongValue)] ? [value longLongValue] : 0;
}

static void DrawOverviewText(NSString *text, NSRect rect, NSFont *font, NSColor *color, NSLineBreakMode mode) {
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

static void DrawOverviewCard(NSRect rect, NSString *label, NSString *value, NSString *detail, NSColor *accent) {
    [[NSColor controlBackgroundColor] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:rect xRadius:12 yRadius:12] fill];
    [accent setFill];
    [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(rect.origin.x, rect.origin.y, 5, rect.size.height)
                                      xRadius:2.5
                                      yRadius:2.5] fill];
    DrawOverviewText(label, NSMakeRect(rect.origin.x + 18, NSMaxY(rect) - 31, rect.size.width - 34, 17),
                     [NSFont systemFontOfSize:13 weight:NSFontWeightMedium], [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingTail);
    DrawOverviewText(value, NSMakeRect(rect.origin.x + 18, rect.origin.y + 34, rect.size.width - 34, 32),
                     [NSFont monospacedDigitSystemFontOfSize:25 weight:NSFontWeightSemibold], [NSColor labelColor],
                     NSLineBreakByTruncatingTail);
    DrawOverviewText(detail, NSMakeRect(rect.origin.x + 18, rect.origin.y + 14, rect.size.width - 34, 16),
                     [NSFont systemFontOfSize:11], [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingTail);
}

static NSArray<NSDictionary *> *SortedServices(NSDictionary *session) {
    NSPredicate *predicate = [NSPredicate predicateWithBlock:^BOOL(NSDictionary *group, NSDictionary *bindings) {
        return [group isKindOfClass:[NSDictionary class]] && ![group[@"id"] isEqualToString:@"unattributed"];
    }];
    NSArray *services = [OverviewArray(session[@"services"]) filteredArrayUsingPredicate:predicate];
    return [services sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *left, NSDictionary *right) {
        long long leftTotal = OverviewNumber(left[@"total_bytes"]);
        long long rightTotal = OverviewNumber(right[@"total_bytes"]);
        if (leftTotal == rightTotal) {
            return NSOrderedSame;
        }
        return leftTotal > rightTotal ? NSOrderedAscending : NSOrderedDescending;
    }];
}

static NSDictionary *RouteByID(NSDictionary *session, NSString *routeID) {
    for (NSDictionary *route in OverviewArray(session[@"routes"])) {
        if ([route isKindOfClass:[NSDictionary class]] && [route[@"id"] isEqualToString:routeID]) {
            return route;
        }
    }
    return @{};
}

void TSDrawTrafficSummaryPanel(NSRect rect, NSDictionary *session, NSDictionary *xrayStats, TSLanguage language) {
    NSDictionary *vps = OverviewDictionary(session[@"vps"]);
    NSDictionary *kernel = OverviewDictionary(session[@"kernel"]);
    NSDictionary *attribution = OverviewDictionary(session[@"attribution"]);
    NSDictionary *breakdown = OverviewDictionary(session[@"breakdown"]);
    CGFloat gap = 12.0;
    CGFloat cardWidth = (rect.size.width - gap * 2.0) / 3.0;
    CGFloat cardHeight = 110.0;
    CGFloat cardY = NSMaxY(rect) - cardHeight;
    DrawOverviewCard(
        NSMakeRect(rect.origin.x, cardY, cardWidth, cardHeight),
        TSLocalized(language, @"dashboard.vps_card"),
        [NSString stringWithFormat:@"T%@", TSFormatBytes(OverviewNumber(vps[@"total_bytes"]))],
        [NSString stringWithFormat:TSLocalized(language, @"dashboard.vps_detail"),
         TSFormatBytes(OverviewNumber(vps[@"in_bytes"])), TSFormatBytes(OverviewNumber(vps[@"out_bytes"]))],
        [NSColor systemOrangeColor]
    );
    DrawOverviewCard(
        NSMakeRect(rect.origin.x + cardWidth + gap, cardY, cardWidth, cardHeight),
        TSLocalized(language, @"dashboard.mihomo_card"),
        TSFormatBytes(OverviewNumber(kernel[@"total_bytes"])),
        [NSString stringWithFormat:TSLocalized(language, @"dashboard.mihomo_detail"),
         TSFormatBytes(OverviewNumber(kernel[@"up_bytes"])), TSFormatBytes(OverviewNumber(kernel[@"down_bytes"]))],
        [NSColor systemPurpleColor]
    );
    DrawOverviewCard(
        NSMakeRect(rect.origin.x + (cardWidth + gap) * 2.0, cardY, cardWidth, cardHeight),
        TSLocalized(language, @"dashboard.proxy_card"),
        TSFormatBytes(OverviewNumber(session[@"proxy_observed_total_bytes"])),
        [NSString stringWithFormat:TSLocalized(language, @"dashboard.proxy_detail"),
         TSFormatBytes(OverviewNumber(attribution[@"unattributed_bytes"]))],
        [NSColor systemBlueColor]
    );

    BOOL empiricalReady = [breakdown[@"empirical_ready"] boolValue];
    NSString *billingLine = empiricalReady
        ? [NSString stringWithFormat:TSLocalized(language, @"estimate.empirical_format"),
           [breakdown[@"observed_multiplier"] doubleValue],
           [breakdown[@"vps_billing_legs"] doubleValue],
           [breakdown[@"billable_overhead_ratio"] doubleValue] * 100.0]
        : TSLocalized(language, @"estimate.empirical_waiting");
    DrawOverviewText(billingLine, NSMakeRect(rect.origin.x + 2, cardY - 29, rect.size.width - 4, 20),
                     [NSFont systemFontOfSize:14 weight:NSFontWeightSemibold],
                     empiricalReady ? [NSColor systemGreenColor] : [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingTail);

    NSString *packagingLine = @"";
    if (empiricalReady && [breakdown[@"packet_breakdown_ready"] boolValue]) {
        packagingLine = [NSString stringWithFormat:TSLocalized(language, @"estimate.packaging_format"),
                         [breakdown[@"billable_overhead_share"] doubleValue] * 100.0,
                         [breakdown[@"packet_overhead_share_of_bill"] doubleValue] * 100.0,
                         [breakdown[@"connection_overhead_share_of_bill"] doubleValue] * 100.0,
                         [breakdown[@"average_packet_bytes"] doubleValue]];
    } else if (empiricalReady) {
        packagingLine = [NSString stringWithFormat:TSLocalized(language, @"estimate.packet_waiting_format"),
                         [breakdown[@"billable_overhead_share"] doubleValue] * 100.0];
    } else {
        packagingLine = TSLocalized(language, @"estimate.alignment_pending");
    }
    DrawOverviewText(packagingLine, NSMakeRect(rect.origin.x + 2, cardY - 49, rect.size.width - 4, 17),
                     [NSFont systemFontOfSize:11.5 weight:NSFontWeightMedium],
                     [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);

    NSRect breakdownRect = NSMakeRect(rect.origin.x, rect.origin.y, rect.size.width, cardY - rect.origin.y - 64);
    [[NSColor controlBackgroundColor] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:breakdownRect xRadius:12 yRadius:12] fill];
    CGFloat inset = 16.0;
    CGFloat dividerX = NSMidX(breakdownRect);
    DrawOverviewText(TSLocalized(language, @"services.title"),
                     NSMakeRect(breakdownRect.origin.x + inset, NSMaxY(breakdownRect) - 28, breakdownRect.size.width / 2.0 - 28, 17),
                     [NSFont systemFontOfSize:13 weight:NSFontWeightMedium], [NSColor labelColor],
                     NSLineBreakByTruncatingTail);
    NSArray *services = OverviewArray(session[@"visible_services"]);
    if (services.count == 0) {
        DrawOverviewText(TSLocalized(language, @"services.waiting"),
                         NSMakeRect(breakdownRect.origin.x + inset, NSMaxY(breakdownRect) - 53, breakdownRect.size.width / 2.0 - 28, 16),
                         [NSFont systemFontOfSize:12], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
    } else {
        NSUInteger limit = MIN((NSUInteger)4, services.count);
        for (NSUInteger index = 0; index < limit; index++) {
            NSDictionary *service = OverviewDictionary(services[index]);
            CGFloat y = NSMaxY(breakdownRect) - 52 - (CGFloat)index * 20.0;
            NSString *line = [NSString stringWithFormat:@"%@   %@",
                              TSLocalizedGroupLabel(language, service),
                              TSFormatBytes(OverviewNumber(service[@"total_bytes"]))];
            DrawOverviewText(line,
                             NSMakeRect(breakdownRect.origin.x + inset, y, breakdownRect.size.width / 2.0 - 28, 16),
                             [NSFont monospacedDigitSystemFontOfSize:12 weight:NSFontWeightRegular], [NSColor labelColor],
                             NSLineBreakByTruncatingTail);
        }
    }
    NSDictionary *direct = RouteByID(session, @"direct");
    NSString *coverage = [NSString stringWithFormat:TSLocalized(language, @"attribution.coverage_format"),
                          [attribution[@"coverage"] doubleValue] * 100.0,
                          TSFormatBytes(OverviewNumber(attribution[@"unattributed_bytes"])),
                          TSFormatBytes(OverviewNumber(direct[@"total_bytes"]))];
    DrawOverviewText(coverage,
                     NSMakeRect(breakdownRect.origin.x + inset, breakdownRect.origin.y + 14,
                                breakdownRect.size.width / 2.0 - 28, 16),
                     [NSFont systemFontOfSize:10.5], [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingTail);

    [[NSColor separatorColor] setStroke];
    NSBezierPath *divider = [NSBezierPath bezierPath];
    [divider moveToPoint:NSMakePoint(dividerX, breakdownRect.origin.y + 14)];
    [divider lineToPoint:NSMakePoint(dividerX, NSMaxY(breakdownRect) - 14)];
    divider.lineWidth = 0.5;
    [divider stroke];

    TSDrawXrayUserTrafficPanel(
        NSMakeRect(dividerX + inset, breakdownRect.origin.y + 10,
                   breakdownRect.size.width / 2.0 - inset * 2.0,
                   breakdownRect.size.height - 20),
        xrayStats,
        language
    );
}

static NSColor *ServiceColor(NSUInteger index) {
    NSArray<NSColor *> *colors = @[
        [NSColor systemBlueColor],
        [NSColor systemGreenColor],
        [NSColor systemTealColor],
    ];
    return colors[index % colors.count];
}

static void DrawLegendItem(CGFloat *x, CGFloat y, NSString *label, NSColor *color) {
    [color setFill];
    [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(*x, y + 4, 7, 7)] fill];
    CGFloat width = MIN(150.0, MAX(48.0, [label sizeWithAttributes:@{NSFontAttributeName: [NSFont systemFontOfSize:11]}].width + 6.0));
    DrawOverviewText(label, NSMakeRect(*x + 11, y, width, 16), [NSFont systemFontOfSize:11],
                     [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
    *x += 11 + width + 10;
}

void TSDrawTrafficTrendPanel(NSRect rect, NSDictionary *session, TSLanguage language) {
    [[NSColor controlBackgroundColor] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:rect xRadius:12 yRadius:12] fill];
    NSDictionary *trend = OverviewDictionary(session[@"trend"]);
    NSArray *buckets = OverviewArray(trend[@"buckets"]);
    NSInteger windowMinutes = MAX(1, [trend[@"window_minutes"] integerValue]);
    DrawOverviewText([NSString stringWithFormat:TSLocalized(language, @"trend.title_format"), (long)windowMinutes],
                     NSMakeRect(rect.origin.x + 16, NSMaxY(rect) - 29, 280, 18),
                     [NSFont systemFontOfSize:13 weight:NSFontWeightMedium], [NSColor labelColor],
                     NSLineBreakByTruncatingTail);
    DrawOverviewText(TSLocalized(language, @"trend.unit"),
                     NSMakeRect(NSMaxX(rect) - 135, NSMaxY(rect) - 29, 119, 18),
                     [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightMedium], [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingHead);

    NSArray *allServices = SortedServices(session);
    NSArray *services = [allServices subarrayWithRange:NSMakeRange(0, MIN((NSUInteger)3, allServices.count))];
    CGFloat legendX = rect.origin.x + 16;
    CGFloat legendY = NSMaxY(rect) - 50;
    for (NSUInteger index = 0; index < services.count; index++) {
        DrawLegendItem(&legendX, legendY, TSLocalizedGroupLabel(language, services[index]), ServiceColor(index));
    }
    DrawLegendItem(&legendX, legendY, TSLocalized(language, @"trend.mihomo"), [NSColor systemPurpleColor]);

    if (buckets.count < 2) {
        DrawOverviewText(TSLocalized(language, @"trend.waiting"),
                         NSMakeRect(rect.origin.x + 62, rect.origin.y + 74, rect.size.width - 84, 20),
                         [NSFont systemFontOfSize:13], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
        return;
    }

    NSRect plot = NSMakeRect(rect.origin.x + 62, rect.origin.y + 30, rect.size.width - 78, rect.size.height - 96);
    long long maximum = MAX(1, OverviewNumber(trend[@"peak_bytes_per_minute"]));
    for (NSInteger index = 0; index < 3; index++) {
        CGFloat y = plot.origin.y + plot.size.height * (CGFloat)index / 2.0;
        [[NSColor separatorColor] setStroke];
        NSBezierPath *grid = [NSBezierPath bezierPath];
        [grid moveToPoint:NSMakePoint(plot.origin.x, y)];
        [grid lineToPoint:NSMakePoint(NSMaxX(plot), y)];
        grid.lineWidth = 0.5;
        [grid stroke];
        double valueMiB = ((double)maximum * (double)index / 2.0) / (1024.0 * 1024.0);
        NSString *tick = valueMiB >= 10.0 ? [NSString stringWithFormat:@"%.0f", valueMiB] : [NSString stringWithFormat:@"%.1f", valueMiB];
        DrawOverviewText(tick, NSMakeRect(rect.origin.x + 12, y - 7, 44, 14),
                         [NSFont monospacedDigitSystemFontOfSize:10 weight:NSFontWeightRegular],
                         [NSColor secondaryLabelColor], NSLineBreakByTruncatingHead);
    }

    double latestEpoch = [OverviewDictionary(buckets.lastObject)[@"epoch"] doubleValue];
    double firstEpoch = latestEpoch - (double)(windowMinutes - 1) * 60.0;
    NSUInteger seriesCount = services.count + 1;
    for (NSUInteger series = 0; series < seriesCount; series++) {
        NSBezierPath *line = [NSBezierPath bezierPath];
        BOOL hasPoint = NO;
        for (NSDictionary *rawPoint in buckets) {
            NSDictionary *point = OverviewDictionary(rawPoint);
            long long value = 0;
            if (series < services.count) {
                NSString *serviceID = [services[series][@"id"] isKindOfClass:[NSString class]] ? services[series][@"id"] : @"";
                value = OverviewNumber(OverviewDictionary(point[@"services"])[serviceID]);
            } else {
                value = OverviewNumber(point[@"mihomo_total"]);
            }
            double epoch = [point[@"epoch"] doubleValue];
            CGFloat x = plot.origin.x + plot.size.width * (CGFloat)MAX(0.0, MIN(1.0, (epoch - firstEpoch) / (latestEpoch - firstEpoch)));
            CGFloat y = plot.origin.y + plot.size.height * (CGFloat)value / (CGFloat)maximum;
            if (!hasPoint) {
                [line moveToPoint:NSMakePoint(x, y)];
                hasPoint = YES;
            } else {
                [line lineToPoint:NSMakePoint(x, y)];
            }
        }
        NSColor *color = series < services.count ? ServiceColor(series) : [NSColor systemPurpleColor];
        [color setStroke];
        line.lineWidth = 1.8;
        [line stroke];
    }
    DrawOverviewText([NSString stringWithFormat:TSLocalized(language, @"trend.ago_format"), (long)windowMinutes],
                     NSMakeRect(plot.origin.x, rect.origin.y + 10, 70, 14),
                     [NSFont systemFontOfSize:10], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
    DrawOverviewText(TSLocalized(language, @"trend.now"),
                     NSMakeRect(NSMaxX(plot) - 42, rect.origin.y + 10, 42, 14),
                     [NSFont systemFontOfSize:10], [NSColor secondaryLabelColor], NSLineBreakByTruncatingHead);
}
