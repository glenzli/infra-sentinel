#import "SettingsController.h"
#import "SettingsStore.h"

static NSDictionary *SettingsDictionary(id value) {
    return [value isKindOfClass:[NSDictionary class]] ? value : @{};
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
@property(nonatomic, strong) NSButton *remoteEnabledButton;
@property(nonatomic, strong) NSTextField *sshHostLabel;
@property(nonatomic, strong) NSTextField *sshHostField;
@property(nonatomic, strong) NSButton *xrayEnabledButton;
@property(nonatomic, strong) NSTextField *billingModeLabel;
@property(nonatomic, strong) NSPopUpButton *billingModePopup;
@property(nonatomic, strong) NSTextField *cycleDayLabel;
@property(nonatomic, strong) NSTextField *cycleDayField;
@property(nonatomic, strong) NSTextField *remoteDetailLabel;
@property(nonatomic, strong) NSTextField *statusLabel;
@property(nonatomic, strong) NSButton *cancelButton;
@property(nonatomic, strong) NSButton *saveButton;
@property(nonatomic, assign) TSLanguage language;
@end

@implementation TSSettingsController

- (instancetype)initWithConfigPath:(NSString *)configPath
                        helperPath:(NSString *)helperPath
                    appliedHandler:(TSSettingsAppliedHandler)appliedHandler {
    self = [super init];
    if (self) {
        _store = [[TSSettingsStore alloc] initWithConfigPath:configPath helperPath:helperPath];
        _appliedHandler = [appliedHandler copy];
        _language = TSDefaultLanguage();
    }
    return self;
}

- (void)createWindowIfNeeded {
    if (self.window != nil) {
        return;
    }
    NSRect frame = NSMakeRect(0, 0, 620, 540);
    self.window = [[NSWindow alloc] initWithContentRect:frame
                                              styleMask:(NSWindowStyleMaskTitled | NSWindowStyleMaskClosable)
                                                backing:NSBackingStoreBuffered
                                                  defer:NO];
    self.window.releasedWhenClosed = NO;
    self.window.tabbingMode = NSWindowTabbingModeDisallowed;
    NSView *content = self.window.contentView;

    self.titleLabel = SettingsLabel(NSMakeRect(24, 490, 572, 30), 24, NSFontWeightBold);
    self.subtitleLabel = SettingsLabel(NSMakeRect(25, 462, 570, 34), 12, NSFontWeightRegular);
    self.subtitleLabel.textColor = [NSColor secondaryLabelColor];
    self.subtitleLabel.maximumNumberOfLines = 2;
    [content addSubview:self.titleLabel];
    [content addSubview:self.subtitleLabel];

    self.monitorBox = [[NSBox alloc] initWithFrame:NSMakeRect(24, 312, 572, 138)];
    [content addSubview:self.monitorBox];
    self.warningLabel = SettingsLabel(NSMakeRect(18, 78, 140, 22), 13, NSFontWeightMedium);
    self.criticalLabel = SettingsLabel(NSMakeRect(18, 39, 140, 22), 13, NSFontWeightMedium);
    self.warningWindowField = SettingsIntegerField(NSMakeRect(165, 76, 58, 24), 1, 120);
    self.warningMinutesLabel = SettingsLabel(NSMakeRect(230, 78, 55, 20), 12, NSFontWeightRegular);
    self.warningThresholdField = SettingsIntegerField(NSMakeRect(330, 76, 92, 24), 1, 1048576);
    self.warningMiBLabel = SettingsLabel(NSMakeRect(430, 78, 55, 20), 12, NSFontWeightRegular);
    self.criticalWindowField = SettingsIntegerField(NSMakeRect(165, 37, 58, 24), 1, 240);
    self.criticalMinutesLabel = SettingsLabel(NSMakeRect(230, 39, 55, 20), 12, NSFontWeightRegular);
    self.criticalThresholdField = SettingsIntegerField(NSMakeRect(330, 37, 92, 24), 1, 1048576);
    self.criticalMiBLabel = SettingsLabel(NSMakeRect(430, 39, 55, 20), 12, NSFontWeightRegular);
    for (NSView *view in @[
        self.warningLabel, self.criticalLabel,
        self.warningWindowField, self.warningMinutesLabel,
        self.warningThresholdField, self.warningMiBLabel,
        self.criticalWindowField, self.criticalMinutesLabel,
        self.criticalThresholdField, self.criticalMiBLabel
    ]) {
        [self.monitorBox addSubview:view];
    }

    self.remoteBox = [[NSBox alloc] initWithFrame:NSMakeRect(24, 102, 572, 198)];
    [content addSubview:self.remoteBox];
    self.remoteEnabledButton = [NSButton checkboxWithTitle:@"" target:self action:@selector(remoteToggled:)];
    self.remoteEnabledButton.frame = NSMakeRect(18, 146, 520, 24);
    self.sshHostLabel = SettingsLabel(NSMakeRect(18, 108, 126, 22), 13, NSFontWeightMedium);
    self.sshHostField = [[NSTextField alloc] initWithFrame:NSMakeRect(154, 106, 270, 24)];
    self.xrayEnabledButton = [NSButton checkboxWithTitle:@"" target:nil action:nil];
    self.xrayEnabledButton.frame = NSMakeRect(154, 73, 370, 24);
    self.billingModeLabel = SettingsLabel(NSMakeRect(18, 39, 126, 22), 13, NSFontWeightMedium);
    self.billingModePopup = [[NSPopUpButton alloc] initWithFrame:NSMakeRect(154, 36, 200, 26) pullsDown:NO];
    self.cycleDayLabel = SettingsLabel(NSMakeRect(360, 39, 110, 22), 13, NSFontWeightMedium);
    self.cycleDayField = SettingsIntegerField(NSMakeRect(482, 37, 54, 24), 1, 31);
    self.remoteDetailLabel = SettingsLabel(NSMakeRect(18, 10, 520, 20), 11, NSFontWeightRegular);
    self.remoteDetailLabel.textColor = [NSColor secondaryLabelColor];
    for (NSView *view in @[
        self.remoteEnabledButton, self.sshHostLabel, self.sshHostField,
        self.xrayEnabledButton, self.billingModeLabel, self.billingModePopup,
        self.cycleDayLabel, self.cycleDayField, self.remoteDetailLabel
    ]) {
        [self.remoteBox addSubview:view];
    }

    self.statusLabel = SettingsLabel(NSMakeRect(26, 60, 568, 34), 11, NSFontWeightRegular);
    self.statusLabel.textColor = [NSColor secondaryLabelColor];
    self.statusLabel.maximumNumberOfLines = 2;
    self.statusLabel.lineBreakMode = NSLineBreakByWordWrapping;
    [content addSubview:self.statusLabel];
    self.cancelButton = [NSButton buttonWithTitle:@"" target:self action:@selector(cancel:)];
    self.cancelButton.bezelStyle = NSBezelStyleRounded;
    self.cancelButton.frame = NSMakeRect(384, 24, 96, 30);
    self.saveButton = [NSButton buttonWithTitle:@"" target:self action:@selector(save:)];
    self.saveButton.bezelStyle = NSBezelStyleRounded;
    self.saveButton.keyEquivalent = @"\r";
    self.saveButton.frame = NSMakeRect(488, 24, 108, 30);
    [content addSubview:self.cancelButton];
    [content addSubview:self.saveButton];
    [self setLanguage:self.language];
}

- (void)setLanguage:(TSLanguage)language {
    _language = language;
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
    self.remoteEnabledButton.title = TSLocalized(language, @"settings.remote_enable");
    self.sshHostLabel.stringValue = TSLocalized(language, @"settings.ssh_alias");
    self.sshHostField.placeholderString = TSLocalized(language, @"settings.ssh_placeholder");
    self.xrayEnabledButton.title = TSLocalized(language, @"settings.xray_enable");
    self.billingModeLabel.stringValue = TSLocalized(language, @"settings.billing_mode");
    self.cycleDayLabel.stringValue = TSLocalized(language, @"settings.cycle_day");
    self.remoteDetailLabel.stringValue = TSLocalized(language, @"settings.remote_fixed");
    self.cancelButton.title = TSLocalized(language, @"settings.cancel");
    self.saveButton.title = TSLocalized(language, @"settings.save");

    NSString *selectedMode = [self.billingModePopup.selectedItem.representedObject isKindOfClass:[NSString class]]
        ? self.billingModePopup.selectedItem.representedObject : @"both";
    [self.billingModePopup removeAllItems];
    [self.billingModePopup addItemWithTitle:TSLocalized(language, @"settings.billing_both")];
    self.billingModePopup.lastItem.representedObject = @"both";
    [self.billingModePopup addItemWithTitle:TSLocalized(language, @"settings.billing_outbound")];
    self.billingModePopup.lastItem.representedObject = @"outbound";
    for (NSMenuItem *item in self.billingModePopup.itemArray) {
        if ([item.representedObject isEqual:selectedMode]) {
            [self.billingModePopup selectItem:item];
            break;
        }
    }
}

- (void)applySettings:(NSDictionary *)settings {
    NSDictionary *monitor = SettingsDictionary(settings[@"monitor"]);
    NSDictionary *remote = SettingsDictionary(settings[@"remote"]);
    self.warningWindowField.integerValue = [monitor[@"warning_window_minutes"] integerValue];
    self.warningThresholdField.integerValue = [monitor[@"warning_mib"] integerValue];
    self.criticalWindowField.integerValue = [monitor[@"critical_window_minutes"] integerValue];
    self.criticalThresholdField.integerValue = [monitor[@"critical_mib"] integerValue];
    self.remoteEnabledButton.state = [remote[@"enabled"] boolValue]
        ? NSControlStateValueOn : NSControlStateValueOff;
    self.sshHostField.stringValue = [remote[@"ssh_host"] isKindOfClass:[NSString class]]
        ? remote[@"ssh_host"] : @"";
    self.xrayEnabledButton.state = [remote[@"xray_stats_enabled"] boolValue]
        ? NSControlStateValueOn : NSControlStateValueOff;
    self.cycleDayField.integerValue = [remote[@"billing_cycle_start_day"] integerValue];
    NSString *billingMode = [remote[@"billing_mode"] isKindOfClass:[NSString class]]
        ? remote[@"billing_mode"] : @"both";
    for (NSMenuItem *item in self.billingModePopup.itemArray) {
        if ([item.representedObject isEqual:billingMode]) {
            [self.billingModePopup selectItem:item];
            break;
        }
    }
    [self updateRemoteControlState];
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
    if (settings == nil) {
        NSError *defaultsError = nil;
        settings = [self.store defaultSettings:&defaultsError];
        self.statusLabel.stringValue = [NSString stringWithFormat:
            TSLocalized(self.language, @"settings.invalid_defaults_format"),
            error.localizedDescription ?: TSLocalized(self.language, @"error.unknown")
        ];
        self.statusLabel.textColor = [NSColor systemOrangeColor];
        if (settings == nil) {
            [self showError:defaultsError ?: error titleKey:@"settings.load_failed"];
            return;
        }
    } else {
        self.statusLabel.stringValue = TSLocalized(self.language, @"settings.status_hint");
        self.statusLabel.textColor = [NSColor secondaryLabelColor];
    }
    [self applySettings:settings];
    [self.window center];
    [self.window makeKeyAndOrderFront:sender];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)updateRemoteControlState {
    BOOL enabled = self.remoteEnabledButton.state == NSControlStateValueOn;
    for (NSControl *control in @[
        self.sshHostField, self.xrayEnabledButton,
        self.billingModePopup, self.cycleDayField
    ]) {
        control.enabled = enabled;
    }
    self.sshHostLabel.textColor = enabled ? [NSColor labelColor] : [NSColor disabledControlTextColor];
    self.billingModeLabel.textColor = enabled ? [NSColor labelColor] : [NSColor disabledControlTextColor];
    self.cycleDayLabel.textColor = enabled ? [NSColor labelColor] : [NSColor disabledControlTextColor];
}

- (void)remoteToggled:(id)sender {
    if (self.remoteEnabledButton.state != NSControlStateValueOn) {
        self.xrayEnabledButton.state = NSControlStateValueOff;
    }
    [self updateRemoteControlState];
}

- (void)cancel:(id)sender {
    [self.window orderOut:sender];
}

- (void)save:(id)sender {
    BOOL remoteEnabled = self.remoteEnabledButton.state == NSControlStateValueOn;
    BOOL xrayEnabled = remoteEnabled && self.xrayEnabledButton.state == NSControlStateValueOn;
    NSString *billingMode = [self.billingModePopup.selectedItem.representedObject isKindOfClass:[NSString class]]
        ? self.billingModePopup.selectedItem.representedObject : @"both";
    NSDictionary *settings = @{
        @"schema": @1,
        @"monitor": @{
            @"warning_window_minutes": @(self.warningWindowField.integerValue),
            @"warning_mib": @(self.warningThresholdField.integerValue),
            @"critical_window_minutes": @(self.criticalWindowField.integerValue),
            @"critical_mib": @(self.criticalThresholdField.integerValue),
        },
        @"remote": @{
            @"enabled": remoteEnabled ? @YES : @NO,
            @"ssh_host": self.sshHostField.stringValue ?: @"",
            @"xray_stats_enabled": xrayEnabled ? @YES : @NO,
            @"billing_cycle_start_day": @(self.cycleDayField.integerValue),
            @"billing_mode": billingMode,
        },
    };
    NSError *error = nil;
    if (![self.store saveSettings:settings error:&error]) {
        [self showError:error titleKey:@"settings.save_failed"];
        return;
    }
    [self.window orderOut:sender];
    if (self.appliedHandler != nil) {
        self.appliedHandler();
    }
}

@end
