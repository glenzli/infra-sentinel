#import "SettingsStore.h"

static NSString *const TSSettingsErrorDomain = @"TrafficSentinel.Settings";
static NSString *const TSSettingsSchema = @"20260808.3";

@interface TSSettingsStore ()
@property(nonatomic, copy) NSString *configPath;
@property(nonatomic, copy) NSString *helperPath;
@property(nonatomic, copy) NSString *pythonSearchPath;
@end

@implementation TSSettingsStore

- (instancetype)initWithConfigPath:(NSString *)configPath
                        helperPath:(NSString *)helperPath
                  pythonSearchPath:(NSString *)pythonSearchPath {
    self = [super init];
    if (self) {
        _configPath = [configPath copy];
        _helperPath = [helperPath copy];
        _pythonSearchPath = [pythonSearchPath copy];
    }
    return self;
}

- (NSError *)errorWithCode:(NSInteger)code message:(NSString *)message {
    return [NSError errorWithDomain:TSSettingsErrorDomain
                               code:code
                           userInfo:@{NSLocalizedDescriptionKey: message ?: @"Settings error"}];
}

- (NSDictionary *)runCommand:(NSString *)command
                        input:(NSDictionary *)input
                        error:(NSError **)error {
    if (self.helperPath.length == 0) {
        if (error != NULL) {
            *error = [self errorWithCode:1 message:@"Settings helper is missing"];
        }
        return nil;
    }

    NSTask *task = [[NSTask alloc] init];
    task.executableURL = [NSURL fileURLWithPath:@"/usr/bin/env"];
    NSMutableArray<NSString *> *arguments = [NSMutableArray arrayWithObjects:
        @"python3", self.helperPath, command, nil
    ];
    if (![command isEqualToString:@"defaults"]) {
        [arguments addObject:self.configPath];
    }
    task.arguments = arguments;
    NSMutableDictionary<NSString *, NSString *> *environment =
        [NSProcessInfo processInfo].environment.mutableCopy;
    environment[@"PATH"] = self.pythonSearchPath;
    task.environment = environment;
    NSPipe *outputPipe = [NSPipe pipe];
    NSPipe *errorPipe = [NSPipe pipe];
    task.standardOutput = outputPipe;
    task.standardError = errorPipe;
    NSPipe *inputPipe = nil;
    NSData *inputData = nil;
    if (input != nil) {
        inputData = [NSJSONSerialization dataWithJSONObject:input options:0 error:error];
        if (inputData == nil) {
            return nil;
        }
        inputPipe = [NSPipe pipe];
        task.standardInput = inputPipe;
    }

    NSError *launchError = nil;
    if (![task launchAndReturnError:&launchError]) {
        if (error != NULL) {
            *error = launchError;
        }
        return nil;
    }
    if (inputPipe != nil) {
        @try {
            [inputPipe.fileHandleForWriting writeData:inputData];
            [inputPipe.fileHandleForWriting closeFile];
        } @catch (NSException *exception) {
            [task terminate];
            if (error != NULL) {
                *error = [self errorWithCode:2 message:exception.reason];
            }
            return nil;
        }
    }
    [task waitUntilExit];

    NSData *output = [outputPipe.fileHandleForReading readDataToEndOfFile];
    NSData *errorOutput = [errorPipe.fileHandleForReading readDataToEndOfFile];
    if (task.terminationStatus != 0) {
        NSString *message = [[NSString alloc] initWithData:errorOutput encoding:NSUTF8StringEncoding];
        message = [message stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
        if (error != NULL) {
            *error = [self errorWithCode:task.terminationStatus
                                 message:message.length > 0 ? message : @"Settings helper failed"];
        }
        return nil;
    }

    NSError *decodeError = nil;
    id payload = [NSJSONSerialization JSONObjectWithData:output options:0 error:&decodeError];
    if (![payload isKindOfClass:[NSDictionary class]] || ![payload[@"schema"] isEqual:TSSettingsSchema]) {
        if (error != NULL) {
            *error = decodeError ?: [self errorWithCode:3 message:@"Settings helper returned invalid data"];
        }
        return nil;
    }
    return payload;
}

- (NSDictionary *)loadSettings:(NSError **)error {
    return [self runCommand:@"export" input:nil error:error];
}

- (NSDictionary *)defaultSettings:(NSError **)error {
    return [self runCommand:@"defaults" input:nil error:error];
}

- (BOOL)saveSettings:(NSDictionary *)settings error:(NSError **)error {
    return [self runCommand:@"write" input:settings error:error] != nil;
}

@end
