#import <Foundation/Foundation.h>
#import "SettingsStore.h"

static void Require(BOOL condition, NSString *message) {
    if (!condition) {
        NSLog(@"%@", message);
        exit(1);
    }
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        Require(argc == 3, @"expected configuration helper and config paths");
        NSString *helperPath = [NSString stringWithUTF8String:argv[1]];
        NSString *configPath = [NSString stringWithUTF8String:argv[2]];
        TSSettingsStore *store = [[TSSettingsStore alloc] initWithConfigPath:configPath
                                                                  helperPath:helperPath
                                                            pythonSearchPath:@"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"];
        NSError *error = nil;
        NSDictionary *defaults = [store defaultSettings:&error];
        Require(defaults != nil, error.localizedDescription ?: @"defaults failed");
        Require([defaults[@"policies"][0][@"warning_mib"] integerValue] == 250,
                @"defaults must cross the native/Python boundary");
        Require([defaults[@"sources"] isKindOfClass:[NSArray class]] && [(NSArray *)defaults[@"sources"] count] == 1,
                @"default source list must contain local Mihomo");

        NSDictionary *settings = @{
            @"schema": @"20260808.3",
            @"app": @{ @"menu_bar_mode": @"health" },
            @"policies": @[@{
                @"id": @"network-traffic-alerts", @"kind": @"traffic.threshold", @"resource_id": @"network",
                @"warning_window_minutes": @7, @"warning_mib": @320,
                @"critical_window_minutes": @12, @"critical_mib": @1536,
            }],
            @"sources": @[@{ @"id": @"local-mihomo", @"kind": @"network.mihomo", @"enabled": @YES }, @{
                @"id": @"primary", @"kind": @"network.linux-xray",
                @"label": @"Primary VPS", @"enabled": @YES,
                @"ssh_host": @"my-vps", @"xray_stats_enabled": @YES,
                @"billing_cycle_start_day": @9, @"billing_mode": @"outbound",
            }],
        };
        Require([store saveSettings:settings error:&error],
                error.localizedDescription ?: @"save failed");
        NSDictionary *loaded = [store loadSettings:&error];
        Require(loaded != nil, error.localizedDescription ?: @"load failed");
        Require([loaded[@"sources"][1][@"billing_mode"] isEqualToString:@"outbound"],
                @"billing mode must survive the bridge");
        Require([loaded[@"policies"][0][@"critical_mib"] integerValue] == 1536,
                @"integer units must survive the bridge");
    }
    return 0;
}
