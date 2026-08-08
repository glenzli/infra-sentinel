#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>
#import "DashboardController.h"
#import "Localization.h"
#import "MonitorHealth.h"
#import "SettingsController.h"
#import "TrafficFormatting.h"

static NSString *const TSPythonSearchPath = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin";
static NSString *const TSProjectionSchema = @"20260808.4";

static NSDictionary *DictionaryValue(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

static NSImage *TSStatusImage(NSString *status) {
    NSImage *image = [[NSImage alloc] initWithSize:NSMakeSize(22, 18)];
    [image lockFocus];
    // A template image inherits the normal macOS menu-bar color. The paired
    // arcs are the product mark; only the center changes with monitor state.
    NSColor *foreground = [NSColor blackColor];
    [foreground setStroke];
    NSBezierPath *leftArc = [NSBezierPath bezierPath];
    [leftArc appendBezierPathWithArcWithCenter:NSMakePoint(10.0, 9.0)
                                         radius:7.3 startAngle:100.0 endAngle:260.0 clockwise:NO];
    leftArc.lineWidth = 1.5;
    [leftArc stroke];
    NSBezierPath *rightArc = [NSBezierPath bezierPath];
    [rightArc appendBezierPathWithArcWithCenter:NSMakePoint(10.0, 9.0)
                                          radius:7.3 startAngle:280.0 endAngle:80.0 clockwise:NO];
    rightArc.lineWidth = 1.5;
    [rightArc stroke];
    [foreground setFill];
    if ([status isEqualToString:@"warning"]) {
        NSBezierPath *mark = [NSBezierPath bezierPath];
        [mark moveToPoint:NSMakePoint(10.0, 12.2)];
        [mark lineToPoint:NSMakePoint(10.0, 8.6)];
        mark.lineWidth = 1.7;
        [mark stroke];
        [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(9.15, 5.6, 1.7, 1.7)] fill];
    } else if ([status isEqualToString:@"critical"] || [status isEqualToString:@"degraded"]) {
        NSBezierPath *cross = [NSBezierPath bezierPath];
        [cross moveToPoint:NSMakePoint(7.4, 6.4)];
        [cross lineToPoint:NSMakePoint(12.6, 11.6)];
        [cross moveToPoint:NSMakePoint(12.6, 6.4)];
        [cross lineToPoint:NSMakePoint(7.4, 11.6)];
        cross.lineWidth = 1.8;
        [cross stroke];
    } else if (![status isEqualToString:@"starting"]) {
        [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(7.7, 6.7, 4.6, 4.6)] fill];
    }
    [image unlockFocus];
    image.template = YES;
    return image;
}

@interface AppDelegate : NSObject <NSApplicationDelegate, UNUserNotificationCenterDelegate>
@property(nonatomic, copy) NSString *supportPath;
@property(nonatomic, copy) NSString *projectionPath;
@property(nonatomic, copy) NSString *configPath;
@property(nonatomic, copy) NSString *agentPath;
@property(nonatomic, copy) NSString *configurationHelperPath;
@property(nonatomic, copy) NSString *notificationStatePath;
@property(nonatomic, copy) NSString *monitorStatus;
@property(nonatomic, strong) NSStatusItem *statusItem;
@property(nonatomic, strong) NSMenu *menu;
@property(nonatomic, strong) NSTimer *refreshTimer;
@property(nonatomic, strong) NSTask *agentTask;
@property(nonatomic, strong) DashboardController *dashboardController;
@property(nonatomic, strong) TSSettingsController *settingsController;
@property(nonatomic, assign) BOOL isQuitting;
@property(nonatomic, assign) BOOL isRestarting;
@property(nonatomic, copy) NSString *lastNotifiedEventID;
@property(nonatomic, assign) BOOL hasLoadedInitialEvent;
@property(nonatomic, assign) TSLanguage language;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSUserDefaults *defaults = [NSUserDefaults standardUserDefaults];
    NSString *storedLanguage = [defaults stringForKey:@"InfraSentinelLanguage"];
    if (storedLanguage.length == 0) {
        storedLanguage = [defaults stringForKey:@"TrafficSentinelLanguage"];
        if (storedLanguage.length > 0) [defaults setObject:storedLanguage forKey:@"InfraSentinelLanguage"];
    }
    self.language = TSLanguageFromIdentifier(storedLanguage);
    [self configurePaths];
    self.menu = [[NSMenu alloc] initWithTitle:@"Infra Sentinel"];
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:30.0];
    self.statusItem.menu = self.menu;
    self.statusItem.button.imagePosition = NSImageOnly;
    BOOL prepared = [self prepareSupportDirectory];
    self.monitorStatus = prepared ? TSLocalized(self.language, @"monitor.starting") : TSLocalized(self.language, @"monitor.init_failed");
    self.dashboardController = [[DashboardController alloc] initWithStateDirectory:[self.supportPath stringByAppendingPathComponent:@"state"]];
    [self.dashboardController setLanguage:self.language];
    __weak typeof(self) weakSelf = self;
    self.settingsController = [[TSSettingsController alloc]
        initWithConfigPath:self.configPath
                helperPath:self.configurationHelperPath
          pythonSearchPath:TSPythonSearchPath
            appliedHandler:^{
                AppDelegate *strongSelf = weakSelf;
                if (strongSelf == nil) {
                    return;
                }
                [strongSelf.dashboardController showNotice:TSLocalized(strongSelf.language, @"notice.settings_applied")];
                [strongSelf restartAgent:nil];
            }];
    [self.settingsController setLanguage:self.language];
    [self.dashboardController setSettingsHandler:^{
        [weakSelf showSettings:nil];
    }];
    [self configureNotifications];
    if (prepared) {
        [self startAgentIfNeeded];
    }
    [self refresh:nil];
    self.refreshTimer = [NSTimer scheduledTimerWithTimeInterval:2.0
                                                          target:self
                                                        selector:@selector(refresh:)
                                                        userInfo:nil
                                                         repeats:YES];
    [[NSRunLoop mainRunLoop] addTimer:self.refreshTimer forMode:NSRunLoopCommonModes];
}

- (void)setStatusIcon:(NSString *)status tooltip:(NSString *)tooltip {
    self.statusItem.button.title = @"";
    self.statusItem.button.image = TSStatusImage(status);
    self.statusItem.button.toolTip = tooltip;
    self.statusItem.button.accessibilityLabel = tooltip;
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    self.isQuitting = YES;
    [self.refreshTimer invalidate];
    [self.agentTask terminate];
}

- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)sender {
    return NO;
}

- (BOOL)applicationShouldHandleReopen:(NSApplication *)sender hasVisibleWindows:(BOOL)flag {
    [self showDashboard:nil];
    return YES;
}

- (void)configurePaths {
    NSURL *applicationSupport = [[NSFileManager defaultManager] URLsForDirectory:NSApplicationSupportDirectory
                                                                         inDomains:NSUserDomainMask].firstObject;
    self.supportPath = [[applicationSupport URLByAppendingPathComponent:@"Infra Sentinel" isDirectory:YES] path];
    self.projectionPath = [self.supportPath stringByAppendingPathComponent:@"state/projection.json"];
    self.configPath = [self.supportPath stringByAppendingPathComponent:@"config.toml"];
    self.notificationStatePath = [self.supportPath stringByAppendingPathComponent:@"notification-state.json"];
    self.agentPath = [[NSBundle mainBundle] pathForResource:@"infra_agent" ofType:@"py" inDirectory:@"Sentinel"];
    self.configurationHelperPath = [[NSBundle mainBundle] pathForResource:@"configuration" ofType:@"py" inDirectory:@"Sentinel"];
}

- (BOOL)prepareSupportDirectory {
    NSFileManager *files = [NSFileManager defaultManager];
    NSError *error = nil;
    NSURL *applicationSupport = [files URLsForDirectory:NSApplicationSupportDirectory inDomains:NSUserDomainMask].firstObject;
    NSString *legacyPath = [[applicationSupport URLByAppendingPathComponent:@"Traffic Sentinel" isDirectory:YES] path];
    if (![files fileExistsAtPath:self.supportPath] && [files fileExistsAtPath:legacyPath]) {
        if (![files moveItemAtPath:legacyPath toPath:self.supportPath error:&error]) return NO;
    }
    if (![files createDirectoryAtPath:[self.supportPath stringByAppendingPathComponent:@"state"]
          withIntermediateDirectories:YES attributes:nil error:&error]) {
        return NO;
    }
    if (![files fileExistsAtPath:self.configPath]) {
        NSString *defaultConfig = [[NSBundle mainBundle] pathForResource:@"config.example" ofType:@"toml" inDirectory:@"Sentinel"];
        if (defaultConfig == nil || ![files copyItemAtPath:defaultConfig toPath:self.configPath error:&error]) {
            return NO;
        }
    }
    return self.agentPath.length > 0 && self.configurationHelperPath.length > 0;
}

- (void)startAgentIfNeeded {
    if (self.agentTask != nil && self.agentTask.running) {
        return;
    }
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    task.arguments = @[ @"python3", self.agentPath, @"--config", self.configPath, @"--watch" ];
    task.currentDirectoryURL = [NSURL fileURLWithPath:self.supportPath isDirectory:YES];
    NSMutableDictionary<NSString *, NSString *> *environment = [NSProcessInfo processInfo].environment.mutableCopy;
    environment[@"PATH"] = TSPythonSearchPath;
    environment[@"PYTHONDONTWRITEBYTECODE"] = @"1";
    environment[@"INFRA_SENTINEL_STATE_DIR"] = [self.supportPath stringByAppendingPathComponent:@"state"];
    environment[@"INFRA_SENTINEL_PARENT_PID"] = [NSString stringWithFormat:@"%d", [NSProcessInfo processInfo].processIdentifier];
    environment[@"INFRA_SENTINEL_APP_NOTIFICATIONS"] = @"1";
    task.environment = environment;
    task.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    task.standardError = [NSFileHandle fileHandleWithNullDevice];

    __weak typeof(self) weakSelf = self;
    task.terminationHandler = ^(NSTask *finishedTask) {
        dispatch_async(dispatch_get_main_queue(), ^{
            AppDelegate *strongSelf = weakSelf;
            if (strongSelf == nil || strongSelf.agentTask != finishedTask) {
                return;
            }
            strongSelf.agentTask = nil;
            if (strongSelf.isQuitting || strongSelf.isRestarting) {
                return;
            }
            strongSelf.monitorStatus = [NSString stringWithFormat:TSLocalized(strongSelf.language, @"monitor.exit_format"), finishedTask.terminationStatus];
            [strongSelf refresh:nil];
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(10 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
                [strongSelf startAgentIfNeeded];
            });
        });
    };

    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        self.monitorStatus = [NSString stringWithFormat:TSLocalized(self.language, @"monitor.launch_failed_format"), error.localizedDescription ?: TSLocalized(self.language, @"error.unknown")];
        return;
    }
    self.agentTask = task;
    self.monitorStatus = TSLocalized(self.language, @"monitor.running");
}

- (NSDictionary *)loadState {
    NSData *data = [NSData dataWithContentsOfFile:self.projectionPath];
    if (data == nil) {
        return nil;
    }
    id parsed = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [parsed isKindOfClass:[NSDictionary class]] && [parsed[@"schema"] isEqual:TSProjectionSchema] ? parsed : nil;
}

- (NSDictionary *)loadHealth {
    NSString *healthPath = [[self.projectionPath stringByDeletingLastPathComponent] stringByAppendingPathComponent:@"health.json"];
    NSData *data = [NSData dataWithContentsOfFile:healthPath];
    id parsed = data == nil ? nil : [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [parsed isKindOfClass:[NSDictionary class]] ? parsed : nil;
}

- (void)configureNotifications {
    NSData *stored = [NSData dataWithContentsOfFile:self.notificationStatePath];
    id payload = stored == nil ? nil : [NSJSONSerialization JSONObjectWithData:stored options:0 error:nil];
    if ([payload isKindOfClass:[NSDictionary class]] && [payload[@"last_event_id"] isKindOfClass:[NSString class]]) {
        self.lastNotifiedEventID = payload[@"last_event_id"];
    }
    UNUserNotificationCenter *center = [UNUserNotificationCenter currentNotificationCenter];
    center.delegate = self;
    [center requestAuthorizationWithOptions:(UNAuthorizationOptionAlert | UNAuthorizationOptionSound) completionHandler:^(BOOL granted, NSError *error) {
        (void)granted;
        (void)error;
    }];
}

- (void)saveLastNotifiedEventID:(NSString *)eventID {
    self.lastNotifiedEventID = eventID;
    NSData *data = [NSJSONSerialization dataWithJSONObject:@{ @"last_event_id": eventID } options:0 error:nil];
    if (data != nil) {
        [data writeToFile:self.notificationStatePath options:NSDataWritingAtomic error:nil];
    }
}

- (NSString *)notificationTitleForEvent:(NSDictionary *)event {
    NSString *group = [event[@"alert_group"] isKindOfClass:[NSString class]] ? event[@"alert_group"] : @"Mihomo";
    NSString *type = [event[@"type"] isKindOfClass:[NSString class]] ? event[@"type"] : @"alert";
    NSString *level = [event[@"level"] isKindOfClass:[NSString class]] ? event[@"level"] : @"warning";
    if ([type isEqualToString:@"recovered"]) {
        return [NSString stringWithFormat:TSLocalized(self.language, @"notification.recovered_title"), group];
    }
    if ([type isEqualToString:@"deescalated"]) {
        return [NSString stringWithFormat:TSLocalized(self.language, @"notification.deescalated_title"), group];
    }
    return [NSString stringWithFormat:TSLocalized(self.language, [level isEqualToString:@"critical"] ? @"notification.critical_title" : @"notification.alert_title"), group];
}

- (NSString *)notificationBodyForEvent:(NSDictionary *)event {
    NSString *type = [event[@"type"] isKindOfClass:[NSString class]] ? event[@"type"] : @"alert";
    if ([type isEqualToString:@"recovered"]) {
        return TSLocalized(self.language, @"notification.recovered_body");
    }
    if ([type isEqualToString:@"deescalated"]) {
        return TSLocalized(self.language, @"notification.deescalated_body");
    }
    if ([event[@"scope"] isEqualToString:@"vps_billing_cycle"]) {
        return [NSString stringWithFormat:TSLocalized(self.language, @"notification.billing_budget_body"),
                TSFormatBytes([event[@"billable_bytes"] longLongValue]),
                TSFormatBytes([event[@"threshold_bytes"] longLongValue])];
    }
    NSDictionary *windows = DictionaryValue(event[@"windows"]);
    NSDictionary *windowSeconds = DictionaryValue(event[@"window_seconds"]);
    NSDictionary *warning = DictionaryValue(windows[@"warning"]);
    NSDictionary *critical = DictionaryValue(windows[@"critical"]);
    NSString *level = [event[@"level"] isKindOfClass:[NSString class]] ? event[@"level"] : @"warning";
    if ([level isEqualToString:@"critical"]) {
        long long total = [critical[@"up_bytes"] longLongValue] + [critical[@"down_bytes"] longLongValue];
        NSInteger minutes = MAX(1, [windowSeconds[@"critical"] integerValue] / 60);
        return [NSString stringWithFormat:TSLocalized(self.language, @"notification.critical_body"), minutes, TSFormatBytes(total)];
    }
    NSInteger minutes = MAX(1, [windowSeconds[@"warning"] integerValue] / 60);
    return [NSString stringWithFormat:TSLocalized(self.language, @"notification.warning_body"), minutes, TSFormatBytes([warning[@"up_bytes"] longLongValue]), TSFormatBytes([warning[@"down_bytes"] longLongValue])];
}

- (void)deliverNotificationForEventIfNeeded:(NSDictionary *)event {
    NSString *eventID = [event[@"id"] isKindOfClass:[NSString class]] ? event[@"id"] : nil;
    if (!self.hasLoadedInitialEvent) {
        self.hasLoadedInitialEvent = YES;
        if (eventID.length > 0) {
            [self saveLastNotifiedEventID:eventID];
        }
        return;
    }
    if (eventID.length == 0 || [eventID isEqualToString:self.lastNotifiedEventID]) {
        return;
    }
    [self saveLastNotifiedEventID:eventID];
    UNMutableNotificationContent *content = [[UNMutableNotificationContent alloc] init];
    content.title = [self notificationTitleForEvent:event];
    content.body = [self notificationBodyForEvent:event];
    content.sound = [UNNotificationSound defaultSound];
    content.userInfo = @{ @"event_id": eventID };
    UNNotificationRequest *request = [UNNotificationRequest requestWithIdentifier:eventID content:content trigger:nil];
    [[UNUserNotificationCenter currentNotificationCenter] addNotificationRequest:request withCompletionHandler:^(NSError *error) {
        (void)error;
    }];
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
 didReceiveNotificationResponse:(UNNotificationResponse *)response
          withCompletionHandler:(void (^)(void))completionHandler {
    dispatch_async(dispatch_get_main_queue(), ^{
        [self showDashboard:nil];
        completionHandler();
    });
}

- (void)userNotificationCenter:(UNUserNotificationCenter *)center
       willPresentNotification:(UNNotification *)notification
          withCompletionHandler:(void (^)(UNNotificationPresentationOptions options))completionHandler {
    completionHandler(UNNotificationPresentationOptionBanner | UNNotificationPresentationOptionSound);
}

- (void)addDisabledItem:(NSString *)title {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:nil keyEquivalent:@""];
    item.enabled = NO;
    [self.menu addItem:item];
}

- (void)addActionItem:(NSString *)title action:(SEL)action {
    NSMenuItem *item = [[NSMenuItem alloc] initWithTitle:title action:action keyEquivalent:@""];
    item.target = self;
    [self.menu addItem:item];
}

- (void)addFooterItems {
    [self.menu addItem:[NSMenuItem separatorItem]];
    [self addActionItem:TSLocalized(self.language, @"menu.restart") action:@selector(restartAgent:)];
    [self addActionItem:TSLocalized(self.language, @"menu.settings") action:@selector(showSettings:)];
    [self addActionItem:TSLocalized(self.language, @"menu.state") action:@selector(showStateFolder:)];
    NSMenuItem *languageItem = [[NSMenuItem alloc] initWithTitle:TSLocalized(self.language, @"menu.language") action:nil keyEquivalent:@""];
    NSMenu *languageMenu = [[NSMenu alloc] initWithTitle:TSLocalized(self.language, @"menu.language")];
    NSMenuItem *chinese = [[NSMenuItem alloc] initWithTitle:TSLocalized(self.language, @"menu.chinese") action:@selector(changeLanguage:) keyEquivalent:@""];
    chinese.target = self;
    chinese.tag = TSLanguageChinese;
    chinese.state = self.language == TSLanguageChinese ? NSControlStateValueOn : NSControlStateValueOff;
    [languageMenu addItem:chinese];
    NSMenuItem *english = [[NSMenuItem alloc] initWithTitle:TSLocalized(self.language, @"menu.english") action:@selector(changeLanguage:) keyEquivalent:@""];
    english.target = self;
    english.tag = TSLanguageEnglish;
    english.state = self.language == TSLanguageEnglish ? NSControlStateValueOn : NSControlStateValueOff;
    [languageMenu addItem:english];
    languageItem.submenu = languageMenu;
    [self.menu addItem:languageItem];
    [self addActionItem:TSLocalized(self.language, @"menu.quit") action:@selector(quit:)];
}

- (void)refresh:(id)sender {
    [self.menu removeAllItems];
    NSDictionary *state = [self loadState];
    NSDictionary *health = [self loadHealth];
    [self.dashboardController updateWithState:TSStateByAttachingMonitorHealth(state, health)];
    if (TSMonitorHealthHasError(health)) {
        NSString *healthError = TSMonitorHealthMessage(health) ?: TSLocalized(self.language, @"error.unknown");
        [self setStatusIcon:@"degraded" tooltip:TSLocalized(self.language, @"infra.status.degraded")];
        [self addActionItem:TSLocalized(self.language, @"menu.open") action:@selector(showDashboard:)];
        [self addDisabledItem:healthError];
        if ([state[@"updated_at"] isKindOfClass:[NSString class]]) {
            [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"status.last_success_format"),
                                   state[@"updated_at"]]];
        }
        [self addFooterItems];
        return;
    }
    if (state == nil) {
        [self setStatusIcon:[self.agentTask isRunning] ? @"starting" : @"degraded"
                     tooltip:[self.agentTask isRunning] ? TSLocalized(self.language, @"status.starting") : TSLocalized(self.language, @"status.abnormal")];
        [self addActionItem:TSLocalized(self.language, @"menu.open") action:@selector(showDashboard:)];
        [self addDisabledItem:TSLocalized(self.language, @"status.first_sample")];
        [self addFooterItems];
        return;
    }
    NSDictionary *infra = DictionaryValue(state[@"infra"]);
    NSDictionary *overall = DictionaryValue(infra[@"overall"]);
    NSDictionary *session = DictionaryValue(state[@"session"]);
    NSString *level = [state[@"level"] isKindOfClass:[NSString class]] ? state[@"level"] : @"none";
    NSString *overallStatus = [overall[@"status"] isKindOfClass:[NSString class]] ? overall[@"status"] : @"healthy";
    NSString *iconStatus = [level isEqualToString:@"critical"] ? @"critical" : ([level isEqualToString:@"warning"] ? @"warning" : overallStatus);
    [self setStatusIcon:iconStatus tooltip:TSLocalized(self.language, [iconStatus isEqualToString:@"healthy"] ? @"infra.status.healthy" : ([iconStatus isEqualToString:@"critical"] ? @"infra.status.critical" : ([iconStatus isEqualToString:@"warning"] ? @"infra.status.warning" : @"infra.status.degraded")))];

    [self addActionItem:TSLocalized(self.language, @"menu.open") action:@selector(showDashboard:)];
    [self addActionItem:TSLocalized(self.language, @"button.reset") action:@selector(resetSession:)];
    NSString *started = [session[@"started_at"] isKindOfClass:[NSString class]] ? session[@"started_at"] : TSLocalized(self.language, @"session.waiting");
    [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"session.menu_format"), started]];
    NSString *statusKey = [overallStatus isEqualToString:@"healthy"]
        ? @"infra.status.healthy"
        : ([overallStatus isEqualToString:@"critical"]
            ? @"infra.status.critical"
            : ([overallStatus isEqualToString:@"warning"] ? @"infra.status.warning" : @"infra.status.degraded"));
    [self addDisabledItem:TSLocalized(self.language, statusKey)];
    NSDictionary *event = DictionaryValue(state[@"last_event"]);
    [self deliverNotificationForEventIfNeeded:event];
    if (event.count > 0) {
        [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"alert.recent_format"), event[@"type"] ?: @"unknown", event[@"level"] ?: @"unknown"]];
    }
    [self addFooterItems];
}

- (void)showDashboard:(id)sender {
    [self.dashboardController showDashboard:sender];
}

- (void)resetSession:(id)sender {
    [self.dashboardController requestSessionReset:sender];
}

- (void)showSettings:(id)sender {
    [self.settingsController showSettings:sender];
}

- (void)restartAgent:(id)sender {
    self.isRestarting = YES;
    self.monitorStatus = TSLocalized(self.language, @"monitor.restarting");
    [self refresh:nil];
    [self.agentTask terminate];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        self.isRestarting = NO;
        [self startAgentIfNeeded];
        [self refresh:nil];
    });
}

- (void)changeLanguage:(NSMenuItem *)sender {
    self.language = sender.tag == TSLanguageEnglish ? TSLanguageEnglish : TSLanguageChinese;
    [[NSUserDefaults standardUserDefaults] setObject:TSLanguageIdentifier(self.language) forKey:@"InfraSentinelLanguage"];
    [self.dashboardController setLanguage:self.language];
    [self.settingsController setLanguage:self.language];
    [self refresh:nil];
}

- (void)showStateFolder:(id)sender {
    NSURL *folder = [NSURL fileURLWithPath:[self.supportPath stringByAppendingPathComponent:@"state"] isDirectory:YES];
    [[NSWorkspace sharedWorkspace] activateFileViewerSelectingURLs:@[ folder ]];
}

- (void)quit:(id)sender {
    [NSApp terminate:nil];
}

@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *application = [NSApplication sharedApplication];
        [application setActivationPolicy:NSApplicationActivationPolicyAccessory];
        AppDelegate *delegate = [[AppDelegate alloc] init];
        application.delegate = delegate;
        [application run];
    }
    return 0;
}
