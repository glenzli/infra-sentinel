#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

FOUNDATION_EXPORT BOOL TSMonitorHealthHasError(NSDictionary * _Nullable health);
FOUNDATION_EXPORT NSString * _Nullable TSMonitorHealthMessage(NSDictionary * _Nullable health);
FOUNDATION_EXPORT NSDictionary *TSStateByAttachingMonitorHealth(
    NSDictionary * _Nullable state,
    NSDictionary * _Nullable health
);

NS_ASSUME_NONNULL_END
