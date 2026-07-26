#import "CodexActivityPanel.h"

static NSDictionary *ActivityDictionary(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

static NSArray *ActivityArray(id value) {
    return [value isKindOfClass:[NSArray class]] ? value : @[];
}

static long long ActivityNumber(id value) {
    return [value respondsToSelector:@selector(longLongValue)] ? [value longLongValue] : 0;
}

static NSString *ActivityBytes(long long value) {
    NSArray<NSString *> *units = @[ @"B", @"KiB", @"MiB", @"GiB", @"TiB" ];
    double number = (double)value;
    for (NSString *unit in units) {
        if (number < 1024.0 || [unit isEqualToString:units.lastObject]) {
            return [unit isEqualToString:@"B"]
                ? [NSString stringWithFormat:@"%lld B", (long long)number]
                : [NSString stringWithFormat:@"%.1f %@", number, unit];
        }
        number /= 1024.0;
    }
    return @"0 B";
}

static void DrawActivityText(NSString *text, NSRect rect, NSFont *font, NSColor *color, NSLineBreakMode mode) {
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

void TSDrawCodexActivityPanel(NSRect rect, NSDictionary *rawActivity, TSLanguage language) {
    NSDictionary *activity = ActivityDictionary(rawActivity);
    [[NSColor controlBackgroundColor] setFill];
    [[NSBezierPath bezierPathWithRoundedRect:rect xRadius:12 yRadius:12] fill];

    BOOL warning = [activity[@"risk"] isEqualToString:@"warning"];
    NSColor *accent = warning ? [NSColor systemOrangeColor] : [NSColor systemBlueColor];
    [accent setFill];
    [[NSBezierPath bezierPathWithRoundedRect:NSMakeRect(rect.origin.x, rect.origin.y, 5, rect.size.height)
                                    xRadius:2.5
                                    yRadius:2.5] fill];
    DrawActivityText(TSLocalized(language, @"activity.title"),
                     NSMakeRect(rect.origin.x + 16, NSMaxY(rect) - 27, 260, 17),
                     [NSFont systemFontOfSize:13 weight:NSFontWeightSemibold],
                     [NSColor labelColor],
                     NSLineBreakByTruncatingTail);
    DrawActivityText(TSLocalized(language, @"activity.privacy"),
                     NSMakeRect(rect.origin.x + 286, NSMaxY(rect) - 27, rect.size.width - 302, 17),
                     [NSFont systemFontOfSize:10],
                     [NSColor tertiaryLabelColor],
                     NSLineBreakByTruncatingTail);

    if (![activity[@"integration_status"] isEqualToString:@"active"]) {
        DrawActivityText(TSLocalized(language, @"activity.waiting"),
                         NSMakeRect(rect.origin.x + 16, rect.origin.y + 61, rect.size.width - 32, 42),
                         [NSFont systemFontOfSize:12],
                         [NSColor secondaryLabelColor],
                         NSLineBreakByWordWrapping);
        return;
    }

    NSString *lastEvent = [activity[@"last_event_at"] isKindOfClass:[NSString class]] ? activity[@"last_event_at"] : @"—";
    DrawActivityText([NSString stringWithFormat:TSLocalized(language, @"activity.last_event_format"), lastEvent],
                     NSMakeRect(rect.origin.x + 16, NSMaxY(rect) - 47, rect.size.width - 32, 16),
                     [NSFont systemFontOfSize:10],
                     [NSColor tertiaryLabelColor],
                     NSLineBreakByTruncatingTail);

    NSString *subagents = [NSString stringWithFormat:TSLocalized(language, @"activity.subagents_format"),
                           ActivityNumber(activity[@"active_subagents"]),
                           ActivityNumber(activity[@"total_subagents"]),
                           ActivityNumber(activity[@"peak_active_subagents"])];
    DrawActivityText(subagents,
                     NSMakeRect(rect.origin.x + 16, NSMaxY(rect) - 68, rect.size.width - 32, 17),
                     [NSFont monospacedDigitSystemFontOfSize:12 weight:NSFontWeightSemibold],
                     warning ? [NSColor systemOrangeColor] : [NSColor labelColor],
                     NSLineBreakByTruncatingTail);

    NSString *tools = [NSString stringWithFormat:TSLocalized(language, @"activity.tools_format"),
                       ActivityNumber(activity[@"tool_calls"]),
                       ActivityNumber(activity[@"read_like_calls"]),
                       ActivityNumber(activity[@"repeated_read_calls"]),
                       ActivityBytes(ActivityNumber(activity[@"tool_response_bytes"]))];
    DrawActivityText(tools,
                     NSMakeRect(rect.origin.x + 16, NSMaxY(rect) - 87, rect.size.width - 32, 16),
                     [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightRegular],
                     [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingTail);

    CGFloat modelX = rect.origin.x + 16;
    CGFloat trafficX = rect.origin.x + rect.size.width * 0.31;
    CGFloat qualityX = rect.origin.x + rect.size.width * 0.56;
    CGFloat activityX = rect.origin.x + rect.size.width * 0.75;
    CGFloat headerY = NSMaxY(rect) - 108;
    DrawActivityText(TSLocalized(language, @"activity.model_header"), NSMakeRect(modelX, headerY, trafficX - modelX - 8, 14), [NSFont systemFontOfSize:10 weight:NSFontWeightMedium], [NSColor tertiaryLabelColor], NSLineBreakByTruncatingTail);
    DrawActivityText(TSLocalized(language, @"activity.traffic_header"), NSMakeRect(trafficX, headerY, qualityX - trafficX - 8, 14), [NSFont systemFontOfSize:10 weight:NSFontWeightMedium], [NSColor tertiaryLabelColor], NSLineBreakByTruncatingTail);
    DrawActivityText(TSLocalized(language, @"activity.quality_header"), NSMakeRect(qualityX, headerY, activityX - qualityX - 8, 14), [NSFont systemFontOfSize:10 weight:NSFontWeightMedium], [NSColor tertiaryLabelColor], NSLineBreakByTruncatingTail);
    DrawActivityText(TSLocalized(language, @"activity.activity_header"), NSMakeRect(activityX, headerY, NSMaxX(rect) - activityX - 16, 14), [NSFont systemFontOfSize:10 weight:NSFontWeightMedium], [NSColor tertiaryLabelColor], NSLineBreakByTruncatingTail);

    NSArray *models = ActivityArray(activity[@"models"]);
    NSUInteger visible = MIN((NSUInteger)3, models.count);
    for (NSUInteger index = 0; index < visible; index++) {
        NSDictionary *model = ActivityDictionary(models[index]);
        CGFloat rowY = headerY - 21.0 * (CGFloat)(index + 1);
        NSString *label = [model[@"label"] isKindOfClass:[NSString class]] ? model[@"label"] : @"Unknown";
        if (ActivityNumber(model[@"active_actors"]) > 0) {
            label = [label stringByAppendingString:@"  ●"];
        }
        DrawActivityText(label, NSMakeRect(modelX, rowY, trafficX - modelX - 8, 17), [NSFont systemFontOfSize:12 weight:NSFontWeightMedium], [NSColor labelColor], NSLineBreakByTruncatingTail);
        DrawActivityText(ActivityBytes(ActivityNumber(model[@"traffic_bytes"])), NSMakeRect(trafficX, rowY, qualityX - trafficX - 8, 17), [NSFont monospacedDigitSystemFontOfSize:12 weight:NSFontWeightMedium], [NSColor labelColor], NSLineBreakByTruncatingTail);

        NSString *quality = TSLocalized(language, @"activity.no_traffic");
        if ([model[@"traffic_quality"] isEqualToString:@"exclusive"]) {
            quality = TSLocalized(language, @"activity.exclusive");
        } else if ([model[@"traffic_quality"] isEqualToString:@"mixed_estimate"]) {
            double ratio = [model[@"exclusive_ratio"] respondsToSelector:@selector(doubleValue)] ? [model[@"exclusive_ratio"] doubleValue] * 100.0 : 0.0;
            quality = [NSString stringWithFormat:TSLocalized(language, @"activity.mixed_format"), ratio];
        }
        DrawActivityText(quality, NSMakeRect(qualityX, rowY, activityX - qualityX - 8, 17), [NSFont systemFontOfSize:11], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
        NSString *counts = [NSString stringWithFormat:TSLocalized(language, @"activity.model_format"), ActivityNumber(model[@"tool_calls"]), ActivityNumber(model[@"subagents"])];
        DrawActivityText(counts, NSMakeRect(activityX, rowY, NSMaxX(rect) - activityX - 16, 17), [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightRegular], [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
    }

    long long unassigned = ActivityNumber(activity[@"unassigned_traffic_bytes"]);
    if (unassigned > 0 && visible < 3) {
        CGFloat rowY = headerY - 21.0 * (CGFloat)(visible + 1);
        DrawActivityText([NSString stringWithFormat:TSLocalized(language, @"activity.unassigned_format"), ActivityBytes(unassigned)],
                         NSMakeRect(modelX, rowY, rect.size.width - 32, 17),
                         [NSFont systemFontOfSize:10],
                         [NSColor tertiaryLabelColor],
                         NSLineBreakByTruncatingTail);
    }
}
