#import "XrayUserPanel.h"
#import "TrafficFormatting.h"

static NSDictionary *XrayDictionary(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

static NSArray *XrayArray(id value) {
    return [value isKindOfClass:[NSArray class]] ? value : @[];
}

static long long XrayNumber(id value) {
    return [value respondsToSelector:@selector(longLongValue)] ? [value longLongValue] : 0;
}

static NSString *XrayString(id value, NSString *fallback) {
    return [value isKindOfClass:[NSString class]] ? value : fallback;
}

static void DrawXrayText(NSString *text, NSRect rect, NSFont *font, NSColor *color, NSLineBreakMode mode) {
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

static NSArray<NSDictionary *> *VisibleXrayUsers(NSArray *users, TSLanguage language) {
    if (users.count <= 4) {
        return users;
    }
    NSMutableArray<NSDictionary *> *visible = [[users subarrayWithRange:NSMakeRange(0, 3)] mutableCopy];
    long long upBytes = 0;
    long long downBytes = 0;
    BOOL flagged = NO;
    for (NSUInteger index = 3; index < users.count; index++) {
        NSDictionary *user = XrayDictionary(users[index]);
        upBytes += XrayNumber(user[@"up_bytes"]);
        downBytes += XrayNumber(user[@"down_bytes"]);
        flagged = flagged || [user[@"flagged"] boolValue];
    }
    [visible addObject:@{
        @"label": [NSString stringWithFormat:TSLocalized(language, @"xray.other_users_format"), (long)(users.count - 3)],
        @"up_bytes": @(upBytes),
        @"down_bytes": @(downBytes),
        @"total_bytes": @(upBytes + downBytes),
        @"flagged": @(flagged),
    }];
    return visible;
}

static NSString *XrayFooter(NSDictionary *state, TSLanguage language) {
    NSString *status = XrayString(state[@"status"], @"waiting");
    if (![state[@"enabled"] boolValue] || [status isEqualToString:@"disabled"]) {
        return TSLocalized(language, @"xray.disabled");
    }
    if ([status isEqualToString:@"error"]) {
        return [NSString stringWithFormat:TSLocalized(language, @"xray.error_format"),
                XrayString(state[@"error"], TSLocalized(language, @"error.unknown"))];
    }
    if (![state[@"ready"] boolValue]) {
        return TSLocalized(language, @"xray.waiting");
    }
    return [NSString stringWithFormat:TSLocalized(language, @"xray.updated_format"),
            XrayString(state[@"updated_at"], TSLocalized(language, @"dashboard.waiting_sample"))];
}

void TSDrawXrayUserTrafficPanel(NSRect rect, NSDictionary *rawState, TSLanguage language) {
    NSDictionary *state = XrayDictionary(rawState);
    NSArray<NSDictionary *> *servers = XrayArray(state[@"remote_servers"]);
    NSArray<NSDictionary *> *users = VisibleXrayUsers(XrayArray(state[@"users"]), language);
    DrawXrayText(TSLocalized(language, @"xray.title"),
                 NSMakeRect(rect.origin.x, NSMaxY(rect) - 19, rect.size.width * 0.55, 17),
                 [NSFont systemFontOfSize:13 weight:NSFontWeightMedium], [NSColor labelColor],
                 NSLineBreakByTruncatingTail);
    NSString *total = [NSString stringWithFormat:TSLocalized(language, @"xray.total_format"),
                       TSFormatBytes(XrayNumber(state[@"total_bytes"]))];
    DrawXrayText(total,
                 NSMakeRect(NSMidX(rect), NSMaxY(rect) - 19, rect.size.width / 2.0, 17),
                 [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightMedium],
                 [NSColor secondaryLabelColor], NSLineBreakByTruncatingHead);

    CGFloat contentTop = NSMaxY(rect) - 44;
    NSUInteger visibleServers = MIN((NSUInteger)3, servers.count);
    if (visibleServers > 0) {
        DrawXrayText(TSLocalized(language, @"remote.title"),
                     NSMakeRect(rect.origin.x, contentTop - 4, rect.size.width, 15),
                     [NSFont systemFontOfSize:11 weight:NSFontWeightMedium], [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingTail);
        for (NSUInteger index = 0; index < visibleServers; index++) {
            NSDictionary *server = XrayDictionary(servers[index]);
            CGFloat y = contentTop - 25 - (CGFloat)index * 19.0;
            NSString *label = XrayString(server[@"label"], XrayString(server[@"id"], @"VPS"));
            NSString *value = TSFormatBytes(XrayNumber(server[@"total_bytes"]));
            // Keep name, direction detail, and total in non-overlapping
            // columns. Direction text is allowed to truncate in its own
            // column when byte values become wide.
            CGFloat labelWidth = MIN(96.0, rect.size.width * 0.32);
            CGFloat totalWidth = MIN(82.0, rect.size.width * 0.26);
            CGFloat detailX = rect.origin.x + labelWidth + 8.0;
            CGFloat detailWidth = MAX(24.0, rect.size.width - labelWidth - totalWidth - 12.0);
            DrawXrayText(label, NSMakeRect(rect.origin.x + 10, y, labelWidth - 10, 16),
                         [NSFont systemFontOfSize:11], [NSColor labelColor], NSLineBreakByTruncatingTail);
            DrawXrayText(value, NSMakeRect(NSMaxX(rect) - totalWidth, y, totalWidth, 16),
                         [NSFont monospacedDigitSystemFontOfSize:10 weight:NSFontWeightMedium], [NSColor labelColor],
                         NSLineBreakByTruncatingHead);
            NSDictionary *vps = XrayDictionary(server[@"vps"]);
            NSString *directions = [NSString stringWithFormat:TSLocalized(language, @"xray.directions_format"),
                                    TSFormatBytes(XrayNumber(vps[@"in_bytes"])), TSFormatBytes(XrayNumber(vps[@"out_bytes"]))];
            DrawXrayText(directions, NSMakeRect(detailX, y, detailWidth, 16),
                         [NSFont monospacedDigitSystemFontOfSize:9 weight:NSFontWeightRegular], [NSColor secondaryLabelColor],
                         NSLineBreakByTruncatingHead);
        }
        contentTop -= 31 + (CGFloat)visibleServers * 19.0;
    }

    if (visibleServers > 0) {
        // The server rows are the primary view when more than one independent
        // remote route is configured; user rows would make this compact panel
        // unreadable. The aggregate remains available in the top total.
    } else if (users.count == 0) {
        DrawXrayText(TSLocalized(language, @"xray.no_users"),
                     NSMakeRect(rect.origin.x, contentTop, rect.size.width, 18),
                     [NSFont systemFontOfSize:12], [NSColor secondaryLabelColor],
                     NSLineBreakByTruncatingTail);
    } else {
        CGFloat labelWidth = MIN(112.0, rect.size.width * 0.30);
        CGFloat detailX = rect.origin.x + labelWidth + 10;
        CGFloat totalWidth = MIN(86.0, rect.size.width * 0.25);
        CGFloat detailWidth = rect.size.width - labelWidth - totalWidth - 18;
        for (NSUInteger index = 0; index < users.count; index++) {
            NSDictionary *user = XrayDictionary(users[index]);
            CGFloat y = contentTop - (CGFloat)index * 21.0;
            BOOL flagged = [user[@"flagged"] boolValue];
            NSColor *accent = flagged ? [NSColor systemOrangeColor] : [NSColor systemIndigoColor];
            [accent setFill];
            [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(rect.origin.x, y + 5, 6, 6)] fill];
            DrawXrayText(XrayString(user[@"label"], TSLocalized(language, @"group.unnamed")),
                         NSMakeRect(rect.origin.x + 10, y, labelWidth - 10, 16),
                         [NSFont systemFontOfSize:11 weight:(flagged ? NSFontWeightSemibold : NSFontWeightRegular)],
                         flagged ? [NSColor systemOrangeColor] : [NSColor labelColor],
                         NSLineBreakByTruncatingTail);
            NSString *directions = [NSString stringWithFormat:TSLocalized(language, @"xray.directions_format"),
                                    TSFormatBytes(XrayNumber(user[@"up_bytes"])),
                                    TSFormatBytes(XrayNumber(user[@"down_bytes"]))];
            DrawXrayText(directions, NSMakeRect(detailX, y, detailWidth, 16),
                         [NSFont monospacedDigitSystemFontOfSize:10 weight:NSFontWeightRegular],
                         [NSColor secondaryLabelColor], NSLineBreakByTruncatingTail);
            DrawXrayText(TSFormatBytes(XrayNumber(user[@"total_bytes"])),
                         NSMakeRect(NSMaxX(rect) - totalWidth, y, totalWidth, 16),
                         [NSFont monospacedDigitSystemFontOfSize:11 weight:NSFontWeightMedium],
                         [NSColor labelColor], NSLineBreakByTruncatingHead);
        }
    }

    DrawXrayText(XrayFooter(state, language),
                 NSMakeRect(rect.origin.x, rect.origin.y, rect.size.width, 16),
                 [NSFont systemFontOfSize:9.5], [NSColor tertiaryLabelColor],
                 NSLineBreakByTruncatingMiddle);
}
