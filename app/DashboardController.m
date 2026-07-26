#import "DashboardController.h"
#import "CodexActivityPanel.h"
#import "TrafficOverviewPanel.h"

static NSDictionary *DashboardDictionary(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

static NSString *DashboardString(id value, NSString *fallback) {
    return [value isKindOfClass:[NSString class]] ? value : fallback;
}

static long long DashboardNumber(id value) {
    return [value respondsToSelector:@selector(longLongValue)] ? [value longLongValue] : 0;
}

static NSString *DashboardDuration(long long seconds, TSLanguage language) {
    long long hours = seconds / 3600;
    long long minutes = (seconds % 3600) / 60;
    long long remainder = seconds % 60;
    if (hours > 0) {
        return [NSString stringWithFormat:TSLocalized(language, @"duration.hours"), hours, minutes];
    }
    if (minutes > 0) {
        return [NSString stringWithFormat:TSLocalized(language, @"duration.minutes"), minutes, remainder];
    }
    return [NSString stringWithFormat:TSLocalized(language, @"duration.seconds"), remainder];
}

static void DrawDashboardText(NSString *text, NSRect rect, NSFont *font, NSColor *color, NSLineBreakMode mode) {
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

@interface TrafficDashboardView : NSView
@property(nonatomic, strong) NSDictionary *dashboardState;
@property(nonatomic, copy) NSString *notice;
@property(nonatomic, assign) TSLanguage language;
@end

@implementation TrafficDashboardView

- (void)setDashboardState:(NSDictionary *)dashboardState {
    _dashboardState = dashboardState;
    [self setNeedsDisplay:YES];
}

- (void)setNotice:(NSString *)notice {
    _notice = [notice copy];
    [self setNeedsDisplay:YES];
}

- (void)setLanguage:(TSLanguage)language {
    _language = language;
    [self setNeedsDisplay:YES];
}

- (void)drawRect:(NSRect)dirtyRect {
    [[NSColor windowBackgroundColor] setFill];
    NSRectFill(self.bounds);
    NSDictionary *state = self.dashboardState ?: @{};
    NSDictionary *session = DashboardDictionary(state[@"session"]);
    CGFloat width = self.bounds.size.width;
    CGFloat height = self.bounds.size.height;

    DrawDashboardText(@"Traffic Sentinel", NSMakeRect(28, height - 50, 360, 30),
                      [NSFont systemFontOfSize:24 weight:NSFontWeightBold], [NSColor labelColor],
                      NSLineBreakByTruncatingTail);
    NSString *started = DashboardString(session[@"started_at"], TSLocalized(self.language, @"dashboard.waiting_sample"));
    NSString *reason = [session[@"started_reason"] isEqualToString:@"manual"]
        ? TSLocalized(self.language, @"session.manual")
        : TSLocalized(self.language, @"session.automatic");
    NSString *header = [NSString stringWithFormat:TSLocalized(self.language, @"session.header_format"),
                        started, reason, DashboardDuration(DashboardNumber(session[@"duration_seconds"]), self.language)];
    DrawDashboardText(header, NSMakeRect(29, height - 74, width - 58, 18),
                      [NSFont systemFontOfSize:12], [NSColor secondaryLabelColor],
                      NSLineBreakByTruncatingTail);

    TSDrawTrafficSummaryPanel(NSMakeRect(28, 538, width - 56, 312), session, self.language);
    TSDrawCodexActivityPanel(NSMakeRect(28, 340, width - 56, 182),
                            DashboardDictionary(state[@"codex_activity"]), self.language);
    TSDrawTrafficTrendPanel(NSMakeRect(28, 58, width - 56, 266), session, self.language);

    NSString *footer = self.notice.length > 0
        ? self.notice
        : [NSString stringWithFormat:TSLocalized(self.language, @"dashboard.updated_format"),
           DashboardString(state[@"updated_at"], TSLocalized(self.language, @"dashboard.waiting_sample"))];
    DrawDashboardText(footer, NSMakeRect(30, 34, width - 60, 18),
                      [NSFont systemFontOfSize:12], [NSColor secondaryLabelColor],
                      NSLineBreakByTruncatingTail);
}

@end

@interface DashboardController ()
@property(nonatomic, copy) NSString *stateDirectory;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) TrafficDashboardView *dashboardView;
@property(nonatomic, strong) NSButton *resetButton;
@property(nonatomic, strong) NSButton *integrationButton;
@property(nonatomic, weak) id integrationTarget;
@property(nonatomic, assign) SEL integrationAction;
@property(nonatomic, assign) TSLanguage language;
@end

@implementation DashboardController

- (instancetype)initWithStateDirectory:(NSString *)stateDirectory
                      integrationTarget:(id)integrationTarget
                      integrationAction:(SEL)integrationAction {
    self = [super init];
    if (self) {
        _stateDirectory = [stateDirectory copy];
        _integrationTarget = integrationTarget;
        _integrationAction = integrationAction;
        _language = TSDefaultLanguage();
    }
    return self;
}

- (void)createWindowIfNeeded {
    if (self.window != nil) {
        return;
    }
    NSRect frame = NSMakeRect(0, 0, 860, 960);
    self.window = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable)
                                                backing:NSBackingStoreBuffered
                                                  defer:NO];
    self.window.title = TSLocalized(self.language, @"window.title");
    self.window.minSize = NSMakeSize(760, 960);
    self.window.releasedWhenClosed = NO;
    self.dashboardView = [[TrafficDashboardView alloc] initWithFrame:frame];
    self.dashboardView.language = self.language;
    self.window.contentView = self.dashboardView;

    self.integrationButton = [NSButton buttonWithTitle:TSLocalized(self.language, @"button.install")
                                                 target:self.integrationTarget
                                                 action:self.integrationAction];
    self.integrationButton.bezelStyle = NSBezelStyleRounded;
    self.integrationButton.font = [NSFont systemFontOfSize:12 weight:NSFontWeightMedium];
    self.integrationButton.frame = NSMakeRect(frame.size.width - 380, frame.size.height - 59, 156, 30);
    self.integrationButton.autoresizingMask = NSViewMinXMargin | NSViewMinYMargin;
    [self.dashboardView addSubview:self.integrationButton];

    self.resetButton = [NSButton buttonWithTitle:TSLocalized(self.language, @"button.reset")
                                           target:self
                                           action:@selector(requestSessionReset:)];
    self.resetButton.bezelStyle = NSBezelStyleRounded;
    self.resetButton.font = [NSFont systemFontOfSize:12 weight:NSFontWeightSemibold];
    self.resetButton.frame = NSMakeRect(frame.size.width - 214, frame.size.height - 59, 184, 30);
    self.resetButton.autoresizingMask = NSViewMinXMargin | NSViewMinYMargin;
    [self.dashboardView addSubview:self.resetButton];
}

- (void)setLanguage:(TSLanguage)language {
    _language = language;
    self.window.title = TSLocalized(language, @"window.title");
    self.dashboardView.language = language;
    self.resetButton.title = TSLocalized(language, @"button.reset");
    self.integrationButton.title = TSLocalized(language, @"button.install");
}

- (void)updateWithState:(NSDictionary *)state {
    [self createWindowIfNeeded];
    self.dashboardView.dashboardState = state ?: @{};
}

- (void)showDashboard:(id)sender {
    [self createWindowIfNeeded];
    [self.window makeKeyAndOrderFront:sender];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)showNotice:(NSString *)notice {
    [self createWindowIfNeeded];
    self.dashboardView.notice = notice;
}

- (void)requestSessionReset:(id)sender {
    NSDictionary *request = @{
        @"schema": @1,
        @"id": [NSUUID UUID].UUIDString,
        @"requested_at": @([[NSDate date] timeIntervalSince1970]),
    };
    NSError *error = nil;
    NSData *data = [NSJSONSerialization dataWithJSONObject:request options:0 error:&error];
    NSString *path = [self.stateDirectory stringByAppendingPathComponent:@"session-reset.request.json"];
    BOOL written = data != nil && [data writeToFile:path options:NSDataWritingAtomic error:&error];
    self.dashboardView.notice = written
        ? TSLocalized(self.language, @"notice.reset")
        : [NSString stringWithFormat:TSLocalized(self.language, @"notice.reset_failed"),
           error.localizedDescription ?: TSLocalized(self.language, @"error.unknown")];
}

@end
