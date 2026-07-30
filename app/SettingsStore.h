#import <Foundation/Foundation.h>

@interface TSSettingsStore : NSObject

- (instancetype)initWithConfigPath:(NSString *)configPath
                        helperPath:(NSString *)helperPath;
- (NSDictionary *)loadSettings:(NSError **)error;
- (NSDictionary *)defaultSettings:(NSError **)error;
- (BOOL)saveSettings:(NSDictionary *)settings error:(NSError **)error;

@end
