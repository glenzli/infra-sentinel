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
                                                                  helperPath:helperPath];
        NSError *error = nil;
        NSDictionary *defaults = [store defaultSettings:&error];
        Require(defaults != nil, error.localizedDescription ?: @"defaults failed");
        Require([defaults[@"monitor"][@"warning_mib"] integerValue] == 250,
                @"defaults must cross the native/Python boundary");
        Require([defaults[@"remote"][@"ssh_host"] isEqualToString:@""],
                @"default SSH alias must stay empty");

        NSDictionary *settings = @{
            @"schema": @1,
            @"monitor": @{
                @"warning_window_minutes": @7,
                @"warning_mib": @320,
                @"critical_window_minutes": @12,
                @"critical_mib": @1536,
            },
            @"remote": @{
                @"enabled": @YES,
                @"ssh_host": @"my-vps",
                @"xray_stats_enabled": @YES,
                @"billing_cycle_start_day": @9,
                @"billing_mode": @"outbound",
            },
        };
        Require([store saveSettings:settings error:&error],
                error.localizedDescription ?: @"save failed");
        NSDictionary *loaded = [store loadSettings:&error];
        Require(loaded != nil, error.localizedDescription ?: @"load failed");
        Require([loaded[@"remote"][@"billing_mode"] isEqualToString:@"outbound"],
                @"billing mode must survive the bridge");
        Require([loaded[@"monitor"][@"critical_mib"] integerValue] == 1536,
                @"integer units must survive the bridge");
    }
    return 0;
}
