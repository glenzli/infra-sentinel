#import "SettingsController.h"
#import "SettingsStore.h"

static NSDictionary *SettingsDictionary(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
}

static NSArray *SettingsArray(id value) {
    return [value isKindOfClass:[NSArray class]] ? value : @[];
}

static NSDictionary *NetworkTrafficPolicy(NSDictionary *settings) {
    for (NSDictionary *policy in SettingsArray(settings[@"policies"])) {
        if ([policy isKindOfClass:[NSDictionary class]] && [policy[@"id"] isEqualToString:@"network-traffic-alerts"]) return policy;
    }
    return @{};
}

static NSArray *RemoteNetworkSources(NSDictionary *settings) {
    NSMutableArray *sources = [NSMutableArray array];
    for (NSDictionary *source in SettingsArray(settings[@"sources"])) {
        if (![source isKindOfClass:[NSDictionary class]] || ![source[@"kind"] isEqualToString:@"network.linux-xray"]) continue;
        NSMutableDictionary *row = [source mutableCopy];
        row[@"billing_alert_enabled"] = @NO;
        row[@"billing_warning_gib"] = @1;
        row[@"billing_critical_gib"] = @2;
        for (NSDictionary *policy in SettingsArray(settings[@"policies"])) {
            if (![policy isKindOfClass:[NSDictionary class]] || ![policy[@"kind"] isEqualToString:@"network.billing.budget"]) continue;
            if (![policy[@"source_id"] isEqual:source[@"id"]]) continue;
            row[@"billing_alert_enabled"] = @YES;
            row[@"billing_warning_gib"] = policy[@"warning_gib"] ?: @1;
            row[@"billing_critical_gib"] = policy[@"critical_gib"] ?: @2;
            break;
        }
        [sources addObject:row];
    }
    return sources;
}

static NSTextField *SettingsLabel(NSRect frame, CGFloat size, NSFontWeight weight) {
    NSTextField *label = [NSTextField labelWithString:@""];
    label.frame = frame;
    label.font = [NSFont systemFontOfSize:size weight:weight];
    label.lineBreakMode = NSLineBreakByTruncatingTail;
    return label;
}

static NSTextField *SettingsIntegerField(NSRect frame, NSInteger minimum, NSInteger maximum) {
    NSTextField *field = [[NSTextField alloc] initWithFrame:frame];
    NSNumberFormatter *formatter = [[NSNumberFormatter alloc] init];
    formatter.numberStyle = NSNumberFormatterDecimalStyle;
    formatter.allowsFloats = NO;
    formatter.minimum = @(minimum);
    formatter.maximum = @(maximum);
    field.formatter = formatter;
    field.alignment = NSTextAlignmentRight;
    return field;
}

@interface TSSettingsController ()
@property(nonatomic, strong) TSSettingsStore *store;
@property(nonatomic, copy) TSSettingsAppliedHandler appliedHandler;
@property(nonatomic, strong) NSWindow *window;
@property(nonatomic, strong) NSTextField *titleLabel;
@property(nonatomic, strong) NSTextField *subtitleLabel;
@property(nonatomic, strong) NSBox *monitorBox;
@property(nonatomic, strong) NSTextField *warningLabel;
@property(nonatomic, strong) NSTextField *criticalLabel;
@property(nonatomic, strong) NSTextField *warningWindowField;
@property(nonatomic, strong) NSTextField *warningThresholdField;
@property(nonatomic, strong) NSTextField *criticalWindowField;
@property(nonatomic, strong) NSTextField *criticalThresholdField;
@property(nonatomic, strong) NSTextField *warningMinutesLabel;
@property(nonatomic, strong) NSTextField *warningMiBLabel;
@property(nonatomic, strong) NSTextField *criticalMinutesLabel;
@property(nonatomic, strong) NSTextField *criticalMiBLabel;
@property(nonatomic, strong) NSBox *remoteBox;
@property(nonatomic, strong) NSScrollView *serverScrollView;
@property(nonatomic, strong) NSView *serverDocumentView;
@property(nonatomic, strong) NSButton *addServerButton;
@property(nonatomic, strong) NSTextField *serverHeaderLabel;
@property(nonatomic, strong) NSTextField *remoteDetailLabel;
@property(nonatomic, strong) NSTextField *statusLabel;
@property(nonatomic, strong) NSButton *cancelButton;
@property(nonatomic, strong) NSButton *saveButton;
@property(nonatomic, strong) NSMutableArray<NSMutableDictionary *> *serverRows;
@property(nonatomic, assign) TSLanguage language;
@end

@implementation TSSettingsController

- (instancetype)initWithConfigPath:(NSString *)configPath
                        helperPath:(NSString *)helperPath
                  pythonSearchPath:(NSString *)pythonSearchPath
                    appliedHandler:(TSSettingsAppliedHandler)appliedHandler {
    self = [super init];
    if (self) {
        _store = [[TSSettingsStore alloc] initWithConfigPath:configPath
                                                 helperPath:helperPath
                                           pythonSearchPath:pythonSearchPath];
        _appliedHandler = [appliedHandler copy];
        _language = TSDefaultLanguage();
        _serverRows = [NSMutableArray array];
    }
    return self;
}

- (void)createWindowIfNeeded {
    if (self.window != nil) return;
    NSRect frame = NSMakeRect(0, 0, 660, 720);
    self.window = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable)
                                                backing:NSBackingStoreBuffered defer:NO];
    self.window.releasedWhenClosed = NO;
    self.window.tabbingMode = NSWindowTabbingModeDisallowed;
    NSView *content = self.window.contentView;

    self.titleLabel = SettingsLabel(NSMakeRect(24, 666, 612, 30), 24, NSFontWeightBold);
    self.subtitleLabel = SettingsLabel(NSMakeRect(25, 632, 610, 34), 12, NSFontWeightRegular);
    self.subtitleLabel.textColor = [NSColor secondaryLabelColor];
    self.subtitleLabel.maximumNumberOfLines = 2;
    [content addSubview:self.titleLabel];
    [content addSubview:self.subtitleLabel];

    // Remote routes are the primary configuration, so keep them directly
    // beneath the title and leave a clear gap before alert thresholds.
    self.monitorBox = [[NSBox alloc] initWithFrame:NSMakeRect(24, 170, 612, 138)];
    [content addSubview:self.monitorBox];
    self.warningLabel = SettingsLabel(NSMakeRect(18, 78, 140, 22), 13, NSFontWeightMedium);
    self.criticalLabel = SettingsLabel(NSMakeRect(18, 39, 140, 22), 13, NSFontWeightMedium);
    self.warningWindowField = SettingsIntegerField(NSMakeRect(175, 76, 58, 24), 1, 120);
    self.warningMinutesLabel = SettingsLabel(NSMakeRect(240, 78, 55, 20), 12, NSFontWeightRegular);
    self.warningThresholdField = SettingsIntegerField(NSMakeRect(340, 76, 92, 24), 1, 1048576);
    self.warningMiBLabel = SettingsLabel(NSMakeRect(440, 78, 55, 20), 12, NSFontWeightRegular);
    self.criticalWindowField = SettingsIntegerField(NSMakeRect(175, 37, 58, 24), 1, 240);
    self.criticalMinutesLabel = SettingsLabel(NSMakeRect(240, 39, 55, 20), 12, NSFontWeightRegular);
    self.criticalThresholdField = SettingsIntegerField(NSMakeRect(340, 37, 92, 24), 1, 1048576);
    self.criticalMiBLabel = SettingsLabel(NSMakeRect(440, 39, 55, 20), 12, NSFontWeightRegular);
    for (NSView *view in @[self.warningLabel, self.criticalLabel, self.warningWindowField,
                           self.warningMinutesLabel, self.warningThresholdField, self.warningMiBLabel,
                           self.criticalWindowField, self.criticalMinutesLabel, self.criticalThresholdField,
                           self.criticalMiBLabel]) [self.monitorBox addSubview:view];

    self.remoteBox = [[NSBox alloc] initWithFrame:NSMakeRect(24, 328, 612, 288)];
    [content addSubview:self.remoteBox];
    self.addServerButton = [NSButton buttonWithTitle:@"" target:self action:@selector(addServer:)];
    self.addServerButton.bezelStyle = NSBezelStyleRounded;
    self.addServerButton.frame = NSMakeRect(454, 247, 124, 25);
    [self.remoteBox addSubview:self.addServerButton];
    self.serverHeaderLabel = SettingsLabel(NSMakeRect(18, 249, 420, 20), 11, NSFontWeightMedium);
    self.serverHeaderLabel.textColor = [NSColor secondaryLabelColor];
    [self.remoteBox addSubview:self.serverHeaderLabel];
    self.serverDocumentView = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 570, 1)];
    self.serverScrollView = [[NSScrollView alloc] initWithFrame:NSMakeRect(18, 48, 576, 190)];
    self.serverScrollView.borderType = NSNoBorder;
    self.serverScrollView.drawsBackground = NO;
    self.serverScrollView.hasVerticalScroller = YES;
    self.serverScrollView.documentView = self.serverDocumentView;
    [self.remoteBox addSubview:self.serverScrollView];
    self.remoteDetailLabel = SettingsLabel(NSMakeRect(18, 13, 576, 28), 11, NSFontWeightRegular);
    self.remoteDetailLabel.textColor = [NSColor secondaryLabelColor];
    self.remoteDetailLabel.maximumNumberOfLines = 2;
    [self.remoteBox addSubview:self.remoteDetailLabel];

    self.statusLabel = SettingsLabel(NSMakeRect(26, 82, 608, 34), 11, NSFontWeightRegular);
    self.statusLabel.textColor = [NSColor secondaryLabelColor];
    self.statusLabel.maximumNumberOfLines = 2;
    self.statusLabel.lineBreakMode = NSLineBreakByWordWrapping;
    [content addSubview:self.statusLabel];
    self.cancelButton = [NSButton buttonWithTitle:@"" target:self action:@selector(cancel:)];
    self.cancelButton.bezelStyle = NSBezelStyleRounded;
    self.cancelButton.frame = NSMakeRect(428, 28, 96, 30);
    self.saveButton = [NSButton buttonWithTitle:@"" target:self action:@selector(save:)];
    self.saveButton.bezelStyle = NSBezelStyleRounded;
    self.saveButton.keyEquivalent = @"\r";
    self.saveButton.frame = NSMakeRect(532, 28, 104, 30);
    [content addSubview:self.cancelButton];
    [content addSubview:self.saveButton];
    [self setLanguage:self.language];
}

- (void)setLanguage:(TSLanguage)language {
    _language = language;
    if (!self.window) return;
    self.window.title = TSLocalized(language, @"settings.window_title");
    self.titleLabel.stringValue = TSLocalized(language, @"settings.title");
    self.subtitleLabel.stringValue = TSLocalized(language, @"settings.subtitle");
    self.monitorBox.title = TSLocalized(language, @"settings.monitor_section");
    self.warningLabel.stringValue = TSLocalized(language, @"settings.warning");
    self.criticalLabel.stringValue = TSLocalized(language, @"settings.critical");
    self.warningMinutesLabel.stringValue = TSLocalized(language, @"settings.minutes");
    self.criticalMinutesLabel.stringValue = TSLocalized(language, @"settings.minutes");
    self.warningMiBLabel.stringValue = TSLocalized(language, @"settings.mib");
    self.criticalMiBLabel.stringValue = TSLocalized(language, @"settings.mib");
    self.remoteBox.title = TSLocalized(language, @"settings.remote_section");
    self.addServerButton.title = TSLocalized(language, @"settings.server_add");
    self.serverHeaderLabel.stringValue = TSLocalized(language, @"settings.server_header");
    self.remoteDetailLabel.stringValue = TSLocalized(language, @"settings.remote_fixed");
    self.cancelButton.title = TSLocalized(language, @"settings.cancel");
    self.saveButton.title = TSLocalized(language, @"settings.save");
    for (NSMutableDictionary *row in self.serverRows) {
        ((NSButton *)row[@"xray"]).title = TSLocalized(language, @"settings.xray_enable_short");
        ((NSButton *)row[@"budget"]).title = TSLocalized(language, @"settings.billing_budget_short");
        ((NSTextField *)row[@"warningBudgetLabel"]).stringValue = TSLocalized(language, @"settings.billing_warning_short");
        ((NSTextField *)row[@"criticalBudgetLabel"]).stringValue = TSLocalized(language, @"settings.billing_critical_short");
        ((NSButton *)row[@"remove"]).title = @"−";
        [self updateBillingPopup:row];
    }
}

- (void)updateBillingPopup:(NSMutableDictionary *)row {
    NSPopUpButton *popup = row[@"billing"];
    NSString *selected = [popup.selectedItem.representedObject isKindOfClass:[NSString class]]
        ? popup.selectedItem.representedObject : @"both";
    [popup removeAllItems];
    [popup addItemWithTitle:TSLocalized(self.language, @"settings.billing_both")];
    popup.lastItem.representedObject = @"both";
    [popup addItemWithTitle:TSLocalized(self.language, @"settings.billing_outbound")];
    popup.lastItem.representedObject = @"outbound";
    for (NSMenuItem *item in popup.itemArray) if ([item.representedObject isEqual:selected]) { [popup selectItem:item]; break; }
}

- (void)reloadServerRows {
    CGFloat rowsHeight = MAX(72.0, MIN(190.0, self.serverRows.count * 72.0));
    // Reserve a real top inset so the header and add button do not touch the
    // rounded panel edge.
    CGFloat remoteHeight = 105.0 + rowsHeight;
    CGFloat remoteTop = 616.0;
    NSRect remoteFrame = NSMakeRect(24, remoteTop - remoteHeight, 612, remoteHeight);
    self.remoteBox.frame = remoteFrame;
    self.addServerButton.frame = NSMakeRect(454, remoteHeight - 60, 124, 25);
    self.serverHeaderLabel.frame = NSMakeRect(18, remoteHeight - 58, 420, 20);
    self.serverScrollView.frame = NSMakeRect(18, 48, 576, rowsHeight);
    // Keep a visible breathing gap between remote reconciliation and alerts.
    self.monitorBox.frame = NSMakeRect(24, remoteFrame.origin.y - 180, 612, 138);
    self.statusLabel.frame = NSMakeRect(26, self.monitorBox.frame.origin.y - 70, 608, 34);
    CGFloat height = MAX(1.0, self.serverRows.count * 72.0);
    self.serverDocumentView.frame = NSMakeRect(0, 0, 556, height);
    for (NSUInteger index = 0; index < self.serverRows.count; index++) {
        NSMutableDictionary *row = self.serverRows[index];
        NSView *view = row[@"view"];
        view.frame = NSMakeRect(0, height - (index + 1) * 72.0, 556, 70);
    }
    [self updateRemoteControlState];
}

- (void)addServerRow:(NSDictionary *)server {
    NSString *serverID = [server[@"id"] isKindOfClass:[NSString class]] ? server[@"id"] : nil;
    if (serverID.length == 0) {
        NSUInteger index = 1;
        while (YES) {
            NSString *candidate = [NSString stringWithFormat:@"vps-%lu", (unsigned long)index++];
            BOOL inUse = NO;
            for (NSDictionary *row in self.serverRows) if ([row[@"id"] isEqual:candidate]) { inUse = YES; break; }
            if (!inUse) { serverID = candidate; break; }
        }
    }
    NSView *view = [[NSView alloc] initWithFrame:NSMakeRect(0, 0, 556, 70)];
    NSButton *enabled = [NSButton checkboxWithTitle:@"" target:self action:@selector(serverToggled:)];
    enabled.frame = NSMakeRect(5, 43, 22, 22);
    enabled.state = [server[@"enabled"] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
    NSTextField *label = [[NSTextField alloc] initWithFrame:NSMakeRect(30, 42, 132, 24)];
    label.placeholderString = TSLocalized(self.language, @"settings.server_label_placeholder");
    label.stringValue = [server[@"label"] isKindOfClass:[NSString class]] ? server[@"label"] : @"";
    NSTextField *ssh = [[NSTextField alloc] initWithFrame:NSMakeRect(168, 42, 130, 24)];
    ssh.placeholderString = TSLocalized(self.language, @"settings.ssh_placeholder");
    ssh.stringValue = [server[@"ssh_host"] isKindOfClass:[NSString class]] ? server[@"ssh_host"] : @"";
    NSButton *xray = [NSButton checkboxWithTitle:TSLocalized(self.language, @"settings.xray_enable_short") target:nil action:nil];
    xray.frame = NSMakeRect(302, 42, 62, 24);
    xray.state = [server[@"xray_stats_enabled"] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
    NSPopUpButton *billing = [[NSPopUpButton alloc] initWithFrame:NSMakeRect(366, 41, 110, 26) pullsDown:NO];
    [view addSubview:enabled]; [view addSubview:label]; [view addSubview:ssh]; [view addSubview:xray]; [view addSubview:billing];
    NSTextField *day = SettingsIntegerField(NSMakeRect(480, 42, 40, 24), 1, 31);
    day.integerValue = [server[@"billing_cycle_start_day"] integerValue] ?: 1;
    [view addSubview:day];
    NSButton *remove = [NSButton buttonWithTitle:@"−" target:self action:@selector(removeServer:)];
    remove.bezelStyle = NSBezelStyleTexturedRounded;
    remove.frame = NSMakeRect(524, 41, 28, 26);
    [view addSubview:remove];
    NSButton *budget = [NSButton checkboxWithTitle:TSLocalized(self.language, @"settings.billing_budget_short") target:self action:@selector(serverBudgetToggled:)];
    budget.frame = NSMakeRect(30, 7, 80, 24);
    budget.state = [server[@"billing_alert_enabled"] boolValue] ? NSControlStateValueOn : NSControlStateValueOff;
    NSTextField *warningBudget = SettingsIntegerField(NSMakeRect(196, 7, 60, 24), 1, 1048576);
    warningBudget.integerValue = [server[@"billing_warning_gib"] integerValue] ?: 1;
    NSTextField *criticalBudget = SettingsIntegerField(NSMakeRect(400, 7, 60, 24), 1, 1048576);
    criticalBudget.integerValue = [server[@"billing_critical_gib"] integerValue] ?: 2;
    NSTextField *warningBudgetLabel = SettingsLabel(NSMakeRect(116, 9, 80, 20), 11, NSFontWeightRegular);
    warningBudgetLabel.stringValue = TSLocalized(self.language, @"settings.billing_warning_short");
    NSTextField *criticalBudgetLabel = SettingsLabel(NSMakeRect(304, 9, 90, 20), 11, NSFontWeightRegular);
    criticalBudgetLabel.stringValue = TSLocalized(self.language, @"settings.billing_critical_short");
    NSTextField *warningUnit = SettingsLabel(NSMakeRect(262, 9, 34, 20), 11, NSFontWeightRegular);
    warningUnit.stringValue = @"GiB";
    NSTextField *criticalUnit = SettingsLabel(NSMakeRect(466, 9, 34, 20), 11, NSFontWeightRegular);
    criticalUnit.stringValue = @"GiB";
    for (NSView *control in @[budget, warningBudget, criticalBudget, warningBudgetLabel, criticalBudgetLabel, warningUnit, criticalUnit]) [view addSubview:control];
    NSMutableDictionary *row = [@{ @"id": serverID, @"view": view, @"enabled": enabled, @"label": label,
                                   @"ssh": ssh, @"xray": xray, @"billing": billing, @"day": day, @"remove": remove,
                                   @"budget": budget, @"warningBudget": warningBudget, @"criticalBudget": criticalBudget,
                                   @"warningBudgetLabel": warningBudgetLabel, @"criticalBudgetLabel": criticalBudgetLabel,
                                   @"warningUnit": warningUnit, @"criticalUnit": criticalUnit } mutableCopy];
    [self.serverRows addObject:row];
    [self.serverDocumentView addSubview:view];
    [self updateBillingPopup:row];
    NSString *mode = [server[@"billing_mode"] isKindOfClass:[NSString class]] ? server[@"billing_mode"] : @"both";
    for (NSMenuItem *item in billing.itemArray) if ([item.representedObject isEqual:mode]) { [billing selectItem:item]; break; }
}

- (void)removeServer:(id)sender {
    for (NSMutableDictionary *row in [self.serverRows copy]) if (row[@"remove"] == sender) {
        [row[@"view"] removeFromSuperview];
        [self.serverRows removeObject:row];
        break;
    }
    [self reloadServerRows];
}

- (void)addServer:(id)sender {
    [self addServerRow:@{ @"enabled": @NO, @"xray_stats_enabled": @NO, @"billing_mode": @"both", @"billing_cycle_start_day": @1,
                          @"billing_alert_enabled": @NO, @"billing_warning_gib": @1, @"billing_critical_gib": @2 }];
    [self reloadServerRows];
}

- (void)updateRemoteControlState {
    for (NSMutableDictionary *row in self.serverRows) {
        BOOL enabled = ((NSButton *)row[@"enabled"]).state == NSControlStateValueOn;
        for (NSControl *control in @[row[@"label"], row[@"ssh"], row[@"xray"], row[@"billing"], row[@"day"], row[@"budget"]]) control.enabled = enabled;
        BOOL budgetEnabled = enabled && ((NSButton *)row[@"budget"]).state == NSControlStateValueOn;
        ((NSControl *)row[@"warningBudget"]).enabled = budgetEnabled;
        ((NSControl *)row[@"criticalBudget"]).enabled = budgetEnabled;
    }
}

- (void)serverToggled:(id)sender {
    for (NSMutableDictionary *row in self.serverRows) {
        if (row[@"enabled"] == sender && ((NSButton *)sender).state != NSControlStateValueOn) {
            ((NSButton *)row[@"xray"]).state = NSControlStateValueOff;
            break;
        }
    }
    [self updateRemoteControlState];
}

- (void)serverBudgetToggled:(id)sender {
    [self updateRemoteControlState];
}

- (void)applySettings:(NSDictionary *)settings {
    NSDictionary *monitor = NetworkTrafficPolicy(settings);
    self.warningWindowField.integerValue = [monitor[@"warning_window_minutes"] integerValue];
    self.warningThresholdField.integerValue = [monitor[@"warning_mib"] integerValue];
    self.criticalWindowField.integerValue = [monitor[@"critical_window_minutes"] integerValue];
    self.criticalThresholdField.integerValue = [monitor[@"critical_mib"] integerValue];
    for (NSMutableDictionary *row in [self.serverRows copy]) [row[@"view"] removeFromSuperview];
    [self.serverRows removeAllObjects];
    for (NSDictionary *server in RemoteNetworkSources(settings)) [self addServerRow:server];
    [self reloadServerRows];
}

- (void)showError:(NSError *)error titleKey:(NSString *)titleKey {
    NSAlert *alert = [[NSAlert alloc] init];
    alert.alertStyle = NSAlertStyleWarning;
    alert.messageText = TSLocalized(self.language, titleKey);
    alert.informativeText = error.localizedDescription ?: TSLocalized(self.language, @"error.unknown");
    [alert addButtonWithTitle:TSLocalized(self.language, @"settings.ok")];
    [alert beginSheetModalForWindow:self.window completionHandler:nil];
}

- (void)showSettings:(id)sender {
    [self createWindowIfNeeded];
    NSError *error = nil;
    NSDictionary *settings = [self.store loadSettings:&error];
    if (!settings) {
        NSError *defaultsError = nil;
        settings = [self.store defaultSettings:&defaultsError];
        self.statusLabel.stringValue = [NSString stringWithFormat:TSLocalized(self.language, @"settings.invalid_defaults_format"), error.localizedDescription ?: TSLocalized(self.language, @"error.unknown")];
        self.statusLabel.textColor = [NSColor systemOrangeColor];
        if (!settings) { [self showError:defaultsError ?: error titleKey:@"settings.load_failed"]; return; }
    } else {
        self.statusLabel.stringValue = TSLocalized(self.language, @"settings.status_hint");
        self.statusLabel.textColor = [NSColor secondaryLabelColor];
    }
    [self applySettings:settings];
    [self.window center];
    [self.window makeKeyAndOrderFront:sender];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)cancel:(id)sender { [self.window orderOut:sender]; }

- (void)save:(id)sender {
    NSMutableArray *servers = [NSMutableArray array];
    NSUInteger index = 0;
    for (NSMutableDictionary *row in self.serverRows) {
        index++;
        NSString *label = [[row[@"label"] stringValue] stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if (!label.length) label = [NSString stringWithFormat:@"VPS %lu", (unsigned long)index];
        NSString *mode = [row[@"billing"] selectedItem].representedObject ?: @"both";
        [servers addObject:@{ @"id": row[@"id"], @"label": label, @"enabled": @([(NSButton *)row[@"enabled"] state] == NSControlStateValueOn),
                              @"ssh_host": [row[@"ssh"] stringValue] ?: @"", @"xray_stats_enabled": @([(NSButton *)row[@"xray"] state] == NSControlStateValueOn),
                              @"billing_cycle_start_day": @([(NSTextField *)row[@"day"] integerValue]), @"billing_mode": mode,
                              @"billing_alert_enabled": @([(NSButton *)row[@"budget"] state] == NSControlStateValueOn),
                              @"billing_warning_gib": @([(NSTextField *)row[@"warningBudget"] integerValue]),
                              @"billing_critical_gib": @([(NSTextField *)row[@"criticalBudget"] integerValue]) }];
    }
    NSMutableArray *sources = [NSMutableArray arrayWithObject:@{ @"id": @"local-mihomo", @"kind": @"network.mihomo", @"enabled": @YES }];
    for (NSDictionary *server in servers) {
        [sources addObject:@{ @"id": server[@"id"], @"kind": @"network.linux-xray", @"label": server[@"label"],
                              @"enabled": server[@"enabled"], @"ssh_host": server[@"ssh_host"],
                              @"xray_stats_enabled": server[@"xray_stats_enabled"],
                              @"billing_cycle_start_day": server[@"billing_cycle_start_day"], @"billing_mode": server[@"billing_mode"] }];
    }
    NSMutableArray *policies = [NSMutableArray arrayWithObject:@{ @"id": @"network-traffic-alerts", @"kind": @"traffic.threshold", @"resource_id": @"network",
        @"warning_window_minutes": @(self.warningWindowField.integerValue), @"warning_mib": @(self.warningThresholdField.integerValue),
        @"critical_window_minutes": @(self.criticalWindowField.integerValue), @"critical_mib": @(self.criticalThresholdField.integerValue) }];
    for (NSDictionary *server in servers) if ([server[@"billing_alert_enabled"] boolValue]) {
        [policies addObject:@{ @"id": [NSString stringWithFormat:@"%@-billing-budget", server[@"id"]],
                               @"kind": @"network.billing.budget", @"source_id": server[@"id"],
                               @"warning_gib": server[@"billing_warning_gib"], @"critical_gib": server[@"billing_critical_gib"] }];
    }
    NSDictionary *settings = @{ @"schema": @"20260808.3", @"app": @{ @"menu_bar_mode": @"health" },
        @"policies": policies,
        @"sources": sources };
    NSError *error = nil;
    if (![self.store saveSettings:settings error:&error]) { [self showError:error titleKey:@"settings.save_failed"]; return; }
    [self.window orderOut:sender];
    if (self.appliedHandler) self.appliedHandler();
}

@end
