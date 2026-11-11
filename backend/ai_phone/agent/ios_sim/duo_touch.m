// Simulator touchscreen routing adapted from duoctl by Karolina Samorek.
// https://github.com/skarol/duoctl · See LICENSE.duoctl.txt.
#import <Foundation/Foundation.h>
#import <dlfcn.h>
#import <mach/mach_time.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

typedef const void *HIDClient;
typedef const void *HIDEvent;
typedef const void *HIDService;
extern HIDClient IOHIDEventSystemClientCreateWithType(CFAllocatorRef, int, CFDictionaryRef);
extern CFArrayRef IOHIDEventSystemClientCopyServices(HIDClient);
extern CFTypeRef IOHIDServiceClientCopyProperty(HIDService, CFStringRef);
extern HIDEvent IOHIDEventCreateDigitizerEvent(CFAllocatorRef, uint64_t, uint32_t, uint32_t,
    uint32_t, uint32_t, uint32_t, double, double, double, double, double, Boolean, Boolean, uint32_t);
extern HIDEvent IOHIDEventCreateDigitizerFingerEvent(CFAllocatorRef, uint64_t, uint32_t, uint32_t,
    uint32_t, double, double, double, double, double, Boolean, Boolean, uint32_t);
extern void IOHIDEventAppendEvent(HIDEvent, HIDEvent, uint32_t);
extern void IOHIDEventSetIntegerValue(HIDEvent, uint32_t, CFIndex);

@interface HIDVirtualEventService : NSObject
- (void)setDelegate:(id)delegate;
- (void)setDispatchQueue:(dispatch_queue_t)queue;
- (void)activate;
- (BOOL)dispatchEvent:(id)event;
- (void)cancel;
@end

static NSDictionary *propertiesForDisplay(NSString *uuid) {
    HIDClient client = IOHIDEventSystemClientCreateWithType(NULL, 1, NULL);
    if (!client) {
        return nil;
    }
    NSArray *services = CFBridgingRelease(IOHIDEventSystemClientCopyServices(client));
    NSDictionary *result = nil;
    for (id service in services) {
        HIDService handle = (__bridge HIDService)service;
        NSString *display = CFBridgingRelease(IOHIDServiceClientCopyProperty(handle, CFSTR("displayUUID")));
        NSNumber *page = CFBridgingRelease(IOHIDServiceClientCopyProperty(handle, CFSTR("PrimaryUsagePage")));
        if (!display || [display caseInsensitiveCompare:uuid] != NSOrderedSame || page.intValue != 0x0D) {
            continue;
        }
        NSMutableDictionary *properties = [NSMutableDictionary dictionary];
        for (NSString *key in @[@"PrimaryUsagePage", @"PrimaryUsage", @"DeviceUsagePairs",
                                 @"Transport", @"Built-In", @"DeviceTypeHint", @"displayUUID"]) {
            id value = CFBridgingRelease(IOHIDServiceClientCopyProperty(handle, (__bridge CFStringRef)key));
            if (value) {
                properties[key] = value;
            }
        }
        properties[@"Product"] = @"aiPhone Duo touchscreen";
        result = properties;
        break;
    }
    CFRelease(client);
    return result;
}

@interface DuoTouchDelegate : NSObject
@property(nonatomic, copy) NSDictionary *properties;
@property(nonatomic, strong) dispatch_semaphore_t ready;
@end

@implementation DuoTouchDelegate
- (id)propertyForKey:(NSString *)key forService:(id)service {
    return self.properties[key];
}
- (BOOL)setProperty:(id)value forKey:(NSString *)key forService:(id)service {
    return YES;
}
- (id)copyEventMatching:(NSDictionary *)matching forService:(id)service {
    return nil;
}
- (BOOL)setOutputEvent:(id)event forService:(id)service {
    return YES;
}
- (void)notification:(NSInteger)type withProperty:(NSDictionary *)property forService:(id)service {
    if (type == 10) {
        dispatch_semaphore_signal(self.ready);
    }
}
@end

static BOOL sendTouch(HIDVirtualEventService *service, double x, double y, BOOL down, BOOL moving) {
    uint64_t stamp = mach_absolute_time();
    uint32_t mask = moving ? (1 << 2) : (1 << 0) | (1 << 1) | (1 << 5);
    HIDEvent hand = IOHIDEventCreateDigitizerEvent(NULL, stamp, 3, 0, 0, mask, 0,
                                                   x, y, 0, 0, 0, down, down, 0);
    HIDEvent finger = IOHIDEventCreateDigitizerFingerEvent(NULL, stamp, 1, 2, mask,
                                                         x, y, 0, down ? 1 : 0, 0, down, down, 0);
    if (!hand || !finger) {
        if (hand) CFRelease(hand);
        if (finger) CFRelease(finger);
        return NO;
    }
    const uint32_t displayIntegrated = (11 << 16) | 25;
    IOHIDEventSetIntegerValue(hand, displayIntegrated, 1);
    IOHIDEventSetIntegerValue(finger, displayIntegrated, 1);
    IOHIDEventAppendEvent(hand, finger, 0);
    BOOL sent = [service dispatchEvent:(__bridge id)hand];
    CFRelease(finger);
    CFRelease(hand);
    return sent;
}

int duoTouch(int argc, char **argv) {
    if (argc != 8) {
        fputs("Expected display UUID, start/end coordinates, duration, count and interval\n", stderr);
        return 2;
    }
    NSDictionary *properties = propertiesForDisplay([NSString stringWithUTF8String:argv[0]]);
    if (!properties || !dlopen("/System/Library/PrivateFrameworks/HID.framework/HID", RTLD_NOW)) {
        fputs("Cannot find the active display's touchscreen\n", stderr);
        return 1;
    }
    __attribute__((objc_precise_lifetime)) DuoTouchDelegate *delegate = [DuoTouchDelegate new];
    delegate.properties = properties;
    delegate.ready = dispatch_semaphore_create(0);
    HIDVirtualEventService *service = [NSClassFromString(@"HIDVirtualEventService") new];
    if (!service) {
        fputs("Simulator touchscreen service is unavailable\n", stderr);
        return 1;
    }
    [service setDelegate:delegate];
    [service setDispatchQueue:dispatch_queue_create("aiPhone.Duo.touch", NULL)];
    [service activate];
    if (dispatch_semaphore_wait(delegate.ready, dispatch_time(DISPATCH_TIME_NOW, 3 * NSEC_PER_SEC))) {
        [service cancel];
        fputs("Simulator did not open the touchscreen\n", stderr);
        return 1;
    }
    double x = atof(argv[1]), y = atof(argv[2]), endX = atof(argv[3]), endY = atof(argv[4]);
    int duration = atoi(argv[5]), count = atoi(argv[6]), interval = atoi(argv[7]);
    int steps = x == endX && y == endY ? 1 : MAX(2, duration * 60 / 1000);
    BOOL sent = YES;
    for (int contact = 0; contact < count; ++contact) {
        if (contact > 0) usleep(interval * 1000);
        sent = sendTouch(service, x, y, YES, NO) && sent;
        for (int step = 1; step <= steps; ++step) {
            usleep(duration * 1000 / steps);
            if (steps > 1) {
                double t = (double)step / steps;
                sent = sendTouch(service, x + (endX - x) * t, y + (endY - y) * t, YES, YES) && sent;
            }
        }
        sent = sendTouch(service, endX, endY, NO, NO) && sent;
    }
    usleep(300000);
    [service cancel];
    if (!sent) fputs("Simulator rejected the touchscreen event\n", stderr);
    return sent ? 0 : 1;
}
