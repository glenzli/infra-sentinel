#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>
#import "DashboardController.h"
#import "Localization.h"
#import "MonitorHealth.h"
#import "SettingsController.h"
#import "TrafficFormatting.h"

static NSDictionary *DictionaryValue(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

@interface AppDelegate : NSObject <NSApplicationDelegate, UNUserNotificationCenterDelegate>
@property(nonatomic, copy) NSString *supportPath;
@property(nonatomic, copy) NSString *statePath;
@property(nonatomic, copy) NSString *configPath;
@property(nonatomic, copy) NSString *helperPath;
@property(nonatomic, copy) NSString *configurationHelperPath;
@property(nonatomic, copy) NSString *notificationStatePath;
@property(nonatomic, copy) NSString *monitorStatus;
@property(nonatomic, strong) NSStatusItem *statusItem;
@property(nonatomic, strong) NSMenu *menu;
@property(nonatomic, strong) NSTimer *refreshTimer;
@property(nonatomic, strong) NSTask *sentinelTask;
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
    NSString *storedLanguage = [[NSUserDefaults standardUserDefaults] stringForKey:@"TrafficSentinelLanguage"];
    self.language = TSLanguageFromIdentifier(storedLanguage);
    [self configurePaths];
    self.menu = [[NSMenu alloc] initWithTitle:@"Traffic Sentinel"];
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    self.statusItem.menu = self.menu;
    BOOL prepared = [self prepareSupportDirectory];
    self.monitorStatus = prepared ? TSLocalized(self.language, @"monitor.starting") : TSLocalized(self.language, @"monitor.init_failed");
    self.dashboardController = [[DashboardController alloc] initWithStateDirectory:[self.supportPath stringByAppendingPathComponent:@"state"]];
    [self.dashboardController setLanguage:self.language];
    __weak typeof(self) weakSelf = self;
    self.settingsController = [[TSSettingsController alloc]
        initWithConfigPath:self.configPath
                helperPath:self.configurationHelperPath
            appliedHandler:^{
                AppDelegate *strongSelf = weakSelf;
                if (strongSelf == nil) {
                    return;
                }
                [strongSelf.dashboardController showNotice:TSLocalized(strongSelf.language, @"notice.settings_applied")];
                [strongSelf restartSentinel:nil];
            }];
    [self.settingsController setLanguage:self.language];
    [self.dashboardController setSettingsHandler:^{
        [weakSelf showSettings:nil];
    }];
    [self configureNotifications];
    if (prepared) {
        [self startSentinelIfNeeded];
    }
    [self refresh:nil];
    self.refreshTimer = [NSTimer scheduledTimerWithTimeInterval:2.0
                                                          target:self
                                                        selector:@selector(refresh:)
                                                        userInfo:nil
                                                         repeats:YES];
    [[NSRunLoop mainRunLoop] addTimer:self.refreshTimer forMode:NSRunLoopCommonModes];
}

- (void)applicationWillTerminate:(NSNotification *)notification {
    self.isQuitting = YES;
    [self.refreshTimer invalidate];
    [self.sentinelTask terminate];
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
    self.supportPath = [[applicationSupport URLByAppendingPathComponent:@"Traffic Sentinel" isDirectory:YES] path];
    self.statePath = [self.supportPath stringByAppendingPathComponent:@"state/menubar.json"];
    self.configPath = [self.supportPath stringByAppendingPathComponent:@"config.toml"];
    self.notificationStatePath = [self.supportPath stringByAppendingPathComponent:@"notification-state.json"];
    self.helperPath = [[NSBundle mainBundle] pathForResource:@"sentinel" ofType:@"py" inDirectory:@"Sentinel"];
    self.configurationHelperPath = [[NSBundle mainBundle] pathForResource:@"configuration" ofType:@"py" inDirectory:@"Sentinel"];
}

- (BOOL)prepareSupportDirectory {
    NSFileManager *files = [NSFileManager defaultManager];
    NSError *error = nil;
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
    return self.helperPath.length > 0 && self.configurationHelperPath.length > 0;
}

- (void)startSentinelIfNeeded {
    if (self.sentinelTask != nil && self.sentinelTask.running) {
        return;
    }
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    task.arguments = @[ @"python3", self.helperPath, @"--config", self.configPath, @"--watch" ];
    task.currentDirectoryURL = [NSURL fileURLWithPath:self.supportPath isDirectory:YES];
    NSMutableDictionary<NSString *, NSString *> *environment = [NSProcessInfo processInfo].environment.mutableCopy;
    environment[@"PATH"] = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin";
    environment[@"PYTHONDONTWRITEBYTECODE"] = @"1";
    environment[@"TRAFFIC_SENTINEL_STATE_DIR"] = [self.supportPath stringByAppendingPathComponent:@"state"];
    environment[@"TRAFFIC_SENTINEL_PARENT_PID"] = [NSString stringWithFormat:@"%d", [NSProcessInfo processInfo].processIdentifier];
    environment[@"TRAFFIC_SENTINEL_APP_NOTIFICATIONS"] = @"1";
    task.environment = environment;
    task.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    task.standardError = [NSFileHandle fileHandleWithNullDevice];

    __weak typeof(self) weakSelf = self;
    task.terminationHandler = ^(NSTask *finishedTask) {
        dispatch_async(dispatch_get_main_queue(), ^{
            AppDelegate *strongSelf = weakSelf;
            if (strongSelf == nil || strongSelf.sentinelTask != finishedTask) {
                return;
            }
            strongSelf.sentinelTask = nil;
            if (strongSelf.isQuitting || strongSelf.isRestarting) {
                return;
            }
            strongSelf.monitorStatus = [NSString stringWithFormat:TSLocalized(strongSelf.language, @"monitor.exit_format"), finishedTask.terminationStatus];
            [strongSelf refresh:nil];
            dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(10 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
                [strongSelf startSentinelIfNeeded];
            });
        });
    };

    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        self.monitorStatus = [NSString stringWithFormat:TSLocalized(self.language, @"monitor.launch_failed_format"), error.localizedDescription ?: TSLocalized(self.language, @"error.unknown")];
        return;
    }
    self.sentinelTask = task;
    self.monitorStatus = TSLocalized(self.language, @"monitor.running");
}

- (NSDictionary *)loadState {
    NSData *data = [NSData dataWithContentsOfFile:self.statePath];
    if (data == nil) {
        return nil;
    }
    id parsed = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [parsed isKindOfClass:[NSDictionary class]] ? parsed : nil;
}

- (NSDictionary *)loadHealth {
    NSString *healthPath = [[self.statePath stringByDeletingLastPathComponent] stringByAppendingPathComponent:@"health.json"];
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
    [self addActionItem:TSLocalized(self.language, @"menu.restart") action:@selector(restartSentinel:)];
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
        self.statusItem.button.title = [@"⚠︎ " stringByAppendingString:TSLocalized(self.language, @"status.sampling_failed")];
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
        self.statusItem.button.title = [self.sentinelTask isRunning]
            ? [@"⌁ " stringByAppendingString:TSLocalized(self.language, @"status.starting")]
            : [@"⚠︎ " stringByAppendingString:TSLocalized(self.language, @"status.abnormal")];
        [self addActionItem:TSLocalized(self.language, @"menu.open") action:@selector(showDashboard:)];
        [self addDisabledItem:TSLocalized(self.language, @"status.first_sample")];
        [self addFooterItems];
        return;
    }
    NSDictionary *vps = DictionaryValue(state[@"vps"]);
    NSDictionary *busiest = DictionaryValue(state[@"busiest_service"]);
    NSDictionary *session = DictionaryValue(state[@"session"]);
    NSDictionary *sessionVps = DictionaryValue(session[@"vps"]);
    NSDictionary *sessionKernel = DictionaryValue(session[@"kernel"]);
    NSDictionary *breakdown = DictionaryValue(session[@"breakdown"]);
    NSString *level = [state[@"level"] isKindOfClass:[NSString class]] ? state[@"level"] : @"none";
    NSString *marker = [level isEqualToString:@"critical"] ? @"⛔" : ([level isEqualToString:@"warning"] ? @"⚠︎" : @"⌁");
    NSString *total = [vps[@"enabled"] boolValue]
        ? TSFormatBytes([sessionVps[@"total_bytes"] longLongValue])
        : TSFormatBytes([sessionKernel[@"total_bytes"] longLongValue]);
    NSString *busiestLabel = busiest.count > 0 ? TSLocalizedGroupLabel(self.language, busiest) : TSLocalized(self.language, @"status.local");
    double observedSeconds = [state[@"observed_seconds"] doubleValue];
    self.statusItem.button.title = [NSString stringWithFormat:@"%@ T%@ · %@ %@", marker, total, busiestLabel, TSFormatRate([busiest[@"up_bytes"] longLongValue] + [busiest[@"down_bytes"] longLongValue], observedSeconds)];

    [self addActionItem:TSLocalized(self.language, @"menu.open") action:@selector(showDashboard:)];
    [self addActionItem:TSLocalized(self.language, @"button.reset") action:@selector(resetSession:)];
    NSString *started = [session[@"started_at"] isKindOfClass:[NSString class]] ? session[@"started_at"] : TSLocalized(self.language, @"session.waiting");
    [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"session.menu_format"), started]];
    if ([breakdown[@"empirical_ready"] boolValue]) {
        [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"estimate.menu_format"),
                               [breakdown[@"observed_multiplier"] doubleValue],
                               [breakdown[@"billable_overhead_share"] doubleValue] * 100.0]];
    } else {
        [self addDisabledItem:TSLocalized(self.language, @"estimate.menu_waiting_format")];
    }
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

- (void)restartSentinel:(id)sender {
    self.isRestarting = YES;
    self.monitorStatus = TSLocalized(self.language, @"monitor.restarting");
    [self refresh:nil];
    [self.sentinelTask terminate];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1 * NSEC_PER_SEC)), dispatch_get_main_queue(), ^{
        self.isRestarting = NO;
        [self startSentinelIfNeeded];
        [self refresh:nil];
    });
}

- (void)changeLanguage:(NSMenuItem *)sender {
    self.language = sender.tag == TSLanguageEnglish ? TSLanguageEnglish : TSLanguageChinese;
    [[NSUserDefaults standardUserDefaults] setObject:TSLanguageIdentifier(self.language) forKey:@"TrafficSentinelLanguage"];
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
