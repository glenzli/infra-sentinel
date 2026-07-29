#import <Foundation/Foundation.h>
#import "MonitorHealth.h"

static void Require(BOOL condition, NSString *message) {
    if (!condition) {
        NSLog(@"%@", message);
        exit(1);
    }
}

int main(void) {
    @autoreleasepool {
        NSDictionary *healthy = @{ @"status": @"ok" };
        NSDictionary *error = @{
            @"status": @"error",
            @"message": @"controller unavailable",
        };
        NSDictionary *state = @{ @"updated_at": @"2026-07-29T10:20:01+08:00" };

        Require(!TSMonitorHealthHasError(nil), @"nil health must not be an error");
        Require(!TSMonitorHealthHasError(healthy), @"healthy state must not be an error");
        Require(TSMonitorHealthHasError(error), @"error health must be detected");
        Require([TSMonitorHealthMessage(error) isEqualToString:@"controller unavailable"],
                @"error message must be preserved");

        NSDictionary *combined = TSStateByAttachingMonitorHealth(state, error);
        Require([combined[@"updated_at"] isEqualToString:state[@"updated_at"]],
                @"the last successful sample must be preserved");
        Require(combined[@"health"] == error, @"health must be attached for dashboard rendering");
    }
    return 0;
}
