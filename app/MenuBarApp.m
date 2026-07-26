#import <Cocoa/Cocoa.h>
#import <UserNotifications/UserNotifications.h>
#import "DashboardController.h"
#import "Localization.h"

static NSString *FormatBytes(long long value) {
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

static NSString *FormatRate(long long value, double seconds) {
    long long perSecond = seconds > 0.0 ? (long long)((double)value / seconds) : value;
    return [NSString stringWithFormat:@"%@/s", FormatBytes(perSecond)];
}

static NSDictionary *DictionaryValue(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

@interface AppDelegate : NSObject <NSApplicationDelegate, UNUserNotificationCenterDelegate>
@property(nonatomic, copy) NSString *supportPath;
@property(nonatomic, copy) NSString *statePath;
@property(nonatomic, copy) NSString *configPath;
@property(nonatomic, copy) NSString *helperPath;
@property(nonatomic, copy) NSString *hookHelperPath;
@property(nonatomic, copy) NSString *migrationHelperPath;
@property(nonatomic, copy) NSString *notificationStatePath;
@property(nonatomic, copy) NSString *monitorStatus;
@property(nonatomic, strong) NSStatusItem *statusItem;
@property(nonatomic, strong) NSMenu *menu;
@property(nonatomic, strong) NSTimer *refreshTimer;
@property(nonatomic, strong) NSTask *sentinelTask;
@property(nonatomic, strong) DashboardController *dashboardController;
@property(nonatomic, assign) BOOL isQuitting;
@property(nonatomic, assign) BOOL isRestarting;
@property(nonatomic, copy) NSString *lastNotifiedEventID;
@property(nonatomic, assign) BOOL hasLoadedInitialEvent;
@property(nonatomic, assign) TSLanguage language;
- (void)checkCodexIntegrationTrustAndOpenReview:(BOOL)openReview;
- (void)openCodexHookReview;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    NSString *storedLanguage = [[NSUserDefaults standardUserDefaults] stringForKey:@"TrafficSentinelLanguage"];
    self.language = TSLanguageFromIdentifier(storedLanguage);
    [self configurePaths];
    self.menu = [[NSMenu alloc] initWithTitle:@"Codex Traffic Sentinel"];
    self.statusItem = [[NSStatusBar systemStatusBar] statusItemWithLength:NSVariableStatusItemLength];
    self.statusItem.menu = self.menu;
    BOOL prepared = [self prepareSupportDirectory];
    self.monitorStatus = prepared ? TSLocalized(self.language, @"monitor.starting") : TSLocalized(self.language, @"monitor.init_failed");
    self.dashboardController = [[DashboardController alloc] initWithStateDirectory:[self.supportPath stringByAppendingPathComponent:@"state"]
                                                                  integrationTarget:self
                                                                  integrationAction:@selector(installCodexIntegration:)];
    [self.dashboardController setLanguage:self.language];
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
    self.supportPath = [[applicationSupport URLByAppendingPathComponent:@"Codex Traffic Sentinel" isDirectory:YES] path];
    self.statePath = [self.supportPath stringByAppendingPathComponent:@"state/menubar.json"];
    self.configPath = [self.supportPath stringByAppendingPathComponent:@"config.toml"];
    self.notificationStatePath = [self.supportPath stringByAppendingPathComponent:@"notification-state.json"];
    self.helperPath = [[NSBundle mainBundle] pathForResource:@"sentinel" ofType:@"py" inDirectory:@"Sentinel"];
    self.hookHelperPath = [[NSBundle mainBundle] pathForResource:@"codex_hook" ofType:@"py" inDirectory:@"Sentinel"];
    self.migrationHelperPath = [[NSBundle mainBundle] pathForResource:@"config_migration" ofType:@"py" inDirectory:@"Sentinel"];
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
    if (self.migrationHelperPath.length == 0) {
        return NO;
    }
    NSTask *migration = [[NSTask alloc] init];
    migration.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    migration.arguments = @[ @"python3", self.migrationHelperPath, self.configPath ];
    migration.standardOutput = [NSFileHandle fileHandleWithNullDevice];
    migration.standardError = [NSFileHandle fileHandleWithNullDevice];
    if (![migration launchAndReturnError:&error]) {
        return NO;
    }
    [migration waitUntilExit];
    return migration.terminationStatus == 0 && self.helperPath.length > 0 && self.hookHelperPath.length > 0;
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
    environment[@"CODEX_TRAFFIC_SENTINEL_STATE_DIR"] = [self.supportPath stringByAppendingPathComponent:@"state"];
    environment[@"CODEX_TRAFFIC_SENTINEL_PARENT_PID"] = [NSString stringWithFormat:@"%d", [NSProcessInfo processInfo].processIdentifier];
    environment[@"CODEX_TRAFFIC_SENTINEL_APP_NOTIFICATIONS"] = @"1";
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
    NSString *group = [event[@"alert_group"] isKindOfClass:[NSString class]] ? event[@"alert_group"] : @"Codex";
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
    NSDictionary *warning = DictionaryValue(windows[@"warning"]);
    NSDictionary *critical = DictionaryValue(windows[@"critical"]);
    NSString *level = [event[@"level"] isKindOfClass:[NSString class]] ? event[@"level"] : @"warning";
    if ([level isEqualToString:@"critical"]) {
        long long total = [critical[@"up_bytes"] longLongValue] + [critical[@"down_bytes"] longLongValue];
        return [NSString stringWithFormat:TSLocalized(self.language, @"notification.critical_body"), FormatBytes(total)];
    }
    return [NSString stringWithFormat:TSLocalized(self.language, @"notification.warning_body"), FormatBytes([warning[@"up_bytes"] longLongValue]), FormatBytes([warning[@"down_bytes"] longLongValue])];
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
    [self addActionItem:TSLocalized(self.language, @"menu.install") action:@selector(installCodexIntegration:)];
    [self addActionItem:TSLocalized(self.language, @"menu.restart") action:@selector(restartSentinel:)];
    [self addActionItem:TSLocalized(self.language, @"menu.edit") action:@selector(openConfig:)];
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
    [self.dashboardController updateWithState:state ?: @{}];
    if (state == nil) {
        NSDictionary *health = [self loadHealth];
        NSString *healthError = [health[@"status"] isEqualToString:@"error"] ? health[@"message"] : nil;
        self.statusItem.button.title = healthError
            ? [@"⚠︎ " stringByAppendingString:TSLocalized(self.language, @"status.sampling_failed")]
            : ([self.sentinelTask isRunning]
                ? [@"⌁ " stringByAppendingString:TSLocalized(self.language, @"status.starting")]
                : [@"⚠︎ " stringByAppendingString:TSLocalized(self.language, @"status.abnormal")]);
        [self addActionItem:TSLocalized(self.language, @"menu.open") action:@selector(showDashboard:)];
        [self addDisabledItem:healthError ?: TSLocalized(self.language, @"status.first_sample")];
        [self addFooterItems];
        return;
    }
    NSDictionary *vps = DictionaryValue(state[@"vps"]);
    NSDictionary *busiest = DictionaryValue(state[@"busiest_group"]);
    NSDictionary *session = DictionaryValue(state[@"session"]);
    NSDictionary *sessionVps = DictionaryValue(session[@"vps"]);
    NSDictionary *breakdown = DictionaryValue(session[@"breakdown"]);
    NSString *level = [state[@"level"] isKindOfClass:[NSString class]] ? state[@"level"] : @"none";
    NSString *marker = [level isEqualToString:@"critical"] ? @"⛔" : ([level isEqualToString:@"warning"] ? @"⚠︎" : @"⌁");
    NSString *vpsTotal = [vps[@"enabled"] boolValue] ? FormatBytes([sessionVps[@"total_bytes"] longLongValue]) : @"—";
    NSString *busiestLabel = busiest.count > 0 ? TSLocalizedGroupLabel(self.language, busiest) : TSLocalized(self.language, @"status.local");
    double observedSeconds = [state[@"observed_seconds"] doubleValue];
    self.statusItem.button.title = [NSString stringWithFormat:@"%@ T%@ · %@ %@", marker, vpsTotal, busiestLabel, FormatRate([busiest[@"up_bytes"] longLongValue] + [busiest[@"down_bytes"] longLongValue], observedSeconds)];

    [self addActionItem:TSLocalized(self.language, @"menu.open") action:@selector(showDashboard:)];
    [self addActionItem:TSLocalized(self.language, @"button.reset") action:@selector(resetSession:)];
    NSString *started = [session[@"started_at"] isKindOfClass:[NSString class]] ? session[@"started_at"] : TSLocalized(self.language, @"session.waiting");
    [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"session.menu_format"), started]];
    double multiplier = [breakdown[@"effective_multiplier"] doubleValue];
    id otherDevices = breakdown[@"other_devices_logical_estimated_bytes"];
    if ([otherDevices respondsToSelector:@selector(longLongValue)]) {
        [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"estimate.menu_format"), multiplier, FormatBytes([otherDevices longLongValue])]];
    } else {
        [self addDisabledItem:[NSString stringWithFormat:TSLocalized(self.language, @"estimate.menu_waiting_format"), multiplier]];
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

- (void)installCodexIntegration:(id)sender {
    if (self.hookHelperPath.length == 0) {
        [self.dashboardController showNotice:[NSString stringWithFormat:TSLocalized(self.language, @"notice.install_failed"), TSLocalized(self.language, @"error.unknown")]];
        return;
    }
    [self.dashboardController showNotice:TSLocalized(self.language, @"notice.installing")];
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    task.arguments = @[ @"python3", self.hookHelperPath, @"--install", @"--support-dir", self.supportPath ];
    NSMutableDictionary<NSString *, NSString *> *environment = [NSProcessInfo processInfo].environment.mutableCopy;
    environment[@"PATH"] = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin";
    environment[@"PYTHONDONTWRITEBYTECODE"] = @"1";
    task.environment = environment;
    NSPipe *output = [NSPipe pipe];
    task.standardOutput = output;
    task.standardError = output;
    __weak typeof(self) weakSelf = self;
    task.terminationHandler = ^(NSTask *finishedTask) {
        NSData *data = [output.fileHandleForReading readDataToEndOfFile];
        NSString *details = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
        dispatch_async(dispatch_get_main_queue(), ^{
            AppDelegate *strongSelf = weakSelf;
            if (strongSelf == nil) {
                return;
            }
            if (finishedTask.terminationStatus == 0) {
                [strongSelf checkCodexIntegrationTrustAndOpenReview:YES];
            } else {
                NSString *message = [details stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
                [strongSelf.dashboardController showNotice:[NSString stringWithFormat:TSLocalized(strongSelf.language, @"notice.install_failed"), message.length > 0 ? message : TSLocalized(strongSelf.language, @"error.unknown")]];
            }
        });
    };
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        [self.dashboardController showNotice:[NSString stringWithFormat:TSLocalized(self.language, @"notice.install_failed"), error.localizedDescription ?: TSLocalized(self.language, @"error.unknown")]];
    }
}

- (void)checkCodexIntegrationTrustAndOpenReview:(BOOL)openReview {
    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    task.arguments = @[ @"python3", self.hookHelperPath, @"--runtime-status", @"--cwd", self.supportPath ];
    NSMutableDictionary<NSString *, NSString *> *environment = [NSProcessInfo processInfo].environment.mutableCopy;
    environment[@"PATH"] = @"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin";
    environment[@"PYTHONDONTWRITEBYTECODE"] = @"1";
    task.environment = environment;
    NSPipe *output = [NSPipe pipe];
    task.standardOutput = output;
    task.standardError = output;
    __weak typeof(self) weakSelf = self;
    task.terminationHandler = ^(NSTask *finishedTask) {
        NSData *data = [output.fileHandleForReading readDataToEndOfFile];
        id parsed = data.length > 0 ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
        NSDictionary *status = [parsed isKindOfClass:[NSDictionary class]] ? parsed : @{};
        dispatch_async(dispatch_get_main_queue(), ^{
            AppDelegate *strongSelf = weakSelf;
            if (strongSelf == nil) {
                return;
            }
            NSString *state = [status[@"status"] isKindOfClass:[NSString class]] ? status[@"status"] : @"error";
            if (finishedTask.terminationStatus == 0 && [state isEqualToString:@"trusted"]) {
                [strongSelf.dashboardController showNotice:TSLocalized(strongSelf.language, @"notice.trusted")];
                return;
            }
            if (finishedTask.terminationStatus == 0 && [state isEqualToString:@"review_required"]) {
                [strongSelf.dashboardController showNotice:TSLocalized(strongSelf.language, @"notice.review_required")];
                if (openReview) {
                    [strongSelf openCodexHookReview];
                }
                return;
            }
            NSString *message = [status[@"error"] isKindOfClass:[NSString class]]
                ? status[@"error"]
                : TSLocalized(strongSelf.language, @"error.unknown");
            [strongSelf.dashboardController showNotice:[NSString stringWithFormat:TSLocalized(strongSelf.language, @"notice.status_failed"), message]];
            if (openReview) {
                [strongSelf openCodexHookReview];
            }
        });
    };
    NSError *error = nil;
    if (![task launchAndReturnError:&error]) {
        [self.dashboardController showNotice:[NSString stringWithFormat:TSLocalized(self.language, @"notice.status_failed"), error.localizedDescription ?: TSLocalized(self.language, @"error.unknown")]];
        if (openReview) {
            [self openCodexHookReview];
        }
    }
}

- (void)openCodexHookReview {
    NSArray<NSString *> *candidates = @[
        @"/Applications/ChatGPT.app/Contents/Resources/codex",
        @"/Applications/Codex.app/Contents/Resources/codex",
    ];
    NSString *codexPath = nil;
    for (NSString *candidate in candidates) {
        if ([[NSFileManager defaultManager] isExecutableFileAtPath:candidate]) {
            codexPath = candidate;
            break;
        }
    }
    if (codexPath == nil) {
        [self.dashboardController showNotice:[NSString stringWithFormat:TSLocalized(self.language, @"notice.review_launch_failed"), TSLocalized(self.language, @"error.unknown")]];
        return;
    }
    NSString *escapedCommand = [NSString stringWithFormat:@"%@ --no-alt-screen", codexPath];
    NSString *source = [NSString stringWithFormat:
        @"tell application \"Terminal\"\n"
         "activate\n"
         "do script \"%@\"\n"
         "end tell",
        escapedCommand
    ];
    NSDictionary *errorInfo = nil;
    NSAppleScript *script = [[NSAppleScript alloc] initWithSource:source];
    if ([script executeAndReturnError:&errorInfo] == nil && errorInfo != nil) {
        NSString *message = [errorInfo[NSAppleScriptErrorMessage] isKindOfClass:[NSString class]]
            ? errorInfo[NSAppleScriptErrorMessage]
            : TSLocalized(self.language, @"error.unknown");
        [self.dashboardController showNotice:[NSString stringWithFormat:TSLocalized(self.language, @"notice.review_launch_failed"), message]];
    }
}

- (void)changeLanguage:(NSMenuItem *)sender {
    self.language = sender.tag == TSLanguageEnglish ? TSLanguageEnglish : TSLanguageChinese;
    [[NSUserDefaults standardUserDefaults] setObject:TSLanguageIdentifier(self.language) forKey:@"TrafficSentinelLanguage"];
    [self.dashboardController setLanguage:self.language];
    [self refresh:nil];
}

- (void)openConfig:(id)sender {
    [[NSWorkspace sharedWorkspace] openURL:[NSURL fileURLWithPath:self.configPath]];
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
