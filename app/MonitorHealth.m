#import "MonitorHealth.h"

BOOL TSMonitorHealthHasError(NSDictionary *health) {
    return [health isKindOfClass:[NSDictionary class]]
        && [health[@"status"] isKindOfClass:[NSString class]]
        && [health[@"status"] isEqualToString:@"error"];
}

NSString *TSMonitorHealthMessage(NSDictionary *health) {
    if (!TSMonitorHealthHasError(health)) {
        return nil;
    }
    NSString *message = [health[@"message"] isKindOfClass:[NSString class]]
        ? health[@"message"]
        : nil;
    return message.length > 0 ? message : nil;
}

NSDictionary *TSStateByAttachingMonitorHealth(NSDictionary *state, NSDictionary *health) {
    NSMutableDictionary *combined = [state isKindOfClass:[NSDictionary class]]
        ? [state mutableCopy]
        : [NSMutableDictionary dictionary];
    if ([health isKindOfClass:[NSDictionary class]] && health.count > 0) {
        combined[@"health"] = health;
    }
    return [combined copy];
}
