#import "TrafficFormatting.h"

NSString *TSFormatBytes(long long value) {
    NSArray<NSString *> *units = @[ @"B", @"KiB", @"MiB", @"GiB", @"TiB" ];
    double number = (double)MAX(0, value);
    for (NSString *unit in units) {
        if (number < 1024.0 || [unit isEqualToString:units.lastObject]) {
            return [unit isEqualToString:@"B"]
                ? [NSString stringWithFormat:@"%lld B", (long long)number]
                : [NSString stringWithFormat:@"%.1f %@", number, unit];
        }
        number /= 1024.0;
    }
    return @"0 B";
}

NSString *TSFormatRate(long long value, double seconds) {
    long long perSecond = seconds > 0.0 ? (long long)((double)value / seconds) : value;
    return [NSString stringWithFormat:@"%@/s", TSFormatBytes(perSecond)];
}
