#import <Cocoa/Cocoa.h>
#import "Localization.h"

@interface DashboardController : NSObject

- (instancetype)initWithStateDirectory:(NSString *)stateDirectory;
- (void)setLanguage:(TSLanguage)language;
- (void)updateWithState:(NSDictionary *)state;
- (void)showDashboard:(id)sender;
- (void)showNotice:(NSString *)notice;
- (void)requestSessionReset:(id)sender;

@end
