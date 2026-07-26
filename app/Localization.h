#import <Cocoa/Cocoa.h>

typedef NS_ENUM(NSInteger, TSLanguage) {
    TSLanguageChinese = 0,
    TSLanguageEnglish = 1,
};

FOUNDATION_EXPORT TSLanguage TSDefaultLanguage(void);
FOUNDATION_EXPORT NSString *TSLanguageIdentifier(TSLanguage language);
FOUNDATION_EXPORT TSLanguage TSLanguageFromIdentifier(NSString *identifier);
FOUNDATION_EXPORT NSString *TSLocalized(TSLanguage language, NSString *key);
FOUNDATION_EXPORT NSString *TSLocalizedGroupLabel(TSLanguage language, NSDictionary *group);

