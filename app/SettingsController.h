#import <Cocoa/Cocoa.h>
#import "Localization.h"

typedef void (^TSSettingsAppliedHandler)(void);

@interface TSSettingsController : NSObject

- (instancetype)initWithConfigPath:(NSString *)configPath
                        helperPath:(NSString *)helperPath
                  pythonSearchPath:(NSString *)pythonSearchPath
                    appliedHandler:(TSSettingsAppliedHandler)appliedHandler;
- (void)setLanguage:(TSLanguage)language;
- (void)showSettings:(id)sender;

@end
