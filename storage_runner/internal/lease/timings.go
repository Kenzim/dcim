package lease

import (
	"fmt"
	"time"

	"golang.org/x/sys/unix"

	"rackflow/storage_runner/internal/api"
)

// Timings are the lease timings of docs/nfs-storage-failover.md section 4.
type Timings struct {
	Lease       time.Duration
	Renew       time.Duration
	StopMargin  time.Duration
	GuardMargin time.Duration
	Poll        time.Duration
	RankStep    time.Duration
	Fence       time.Duration
	// Ganesha NFSv4 Lease_Lifetime and Grace_Period, in seconds.
	NFSLeaseLifetime int64
	NFSGracePeriod   int64
}

func DefaultTimings() Timings {
	return Timings{
		Lease:            20 * time.Second,
		Renew:            4 * time.Second,
		StopMargin:       6 * time.Second,
		GuardMargin:      3 * time.Second,
		Poll:             2 * time.Second,
		RankStep:         6 * time.Second,
		Fence:            600 * time.Second,
		NFSLeaseLifetime: 30,
		NFSGracePeriod:   45,
	}
}

// TimingsFromConfig applies the ha block over the defaults (zero keeps the
// default) and refuses values that would break the protocol.
func TimingsFromConfig(h *api.HA) (Timings, error) {
	t := DefaultTimings()
	if h == nil {
		return t, nil
	}
	set := func(dst *time.Duration, name string, v int64) error {
		if v < 0 {
			return fmt.Errorf("ha.%s must not be negative", name)
		}
		if v > 0 {
			*dst = time.Duration(v) * time.Second
		}
		return nil
	}
	for _, f := range []struct {
		dst  *time.Duration
		name string
		v    int64
	}{
		{&t.Lease, "lease_seconds", h.LeaseSeconds},
		{&t.Renew, "renew_seconds", h.RenewSeconds},
		{&t.StopMargin, "stop_margin_seconds", h.StopMarginSeconds},
		{&t.GuardMargin, "guard_margin_seconds", h.GuardMarginSeconds},
		{&t.Poll, "poll_seconds", h.PollSeconds},
		{&t.RankStep, "rank_step_seconds", h.RankStepSeconds},
		{&t.Fence, "fence_seconds", h.FenceSeconds},
	} {
		if err := set(f.dst, f.name, f.v); err != nil {
			return DefaultTimings(), err
		}
	}
	if h.NFSLeaseLifetime < 0 || h.NFSGracePeriod < 0 {
		return DefaultTimings(), fmt.Errorf("ha.nfs_lease_lifetime and ha.nfs_grace_period must not be negative")
	}
	if h.NFSLeaseLifetime > 0 {
		t.NFSLeaseLifetime = h.NFSLeaseLifetime
	}
	if h.NFSGracePeriod > 0 {
		t.NFSGracePeriod = h.NFSGracePeriod
	}
	switch {
	case t.StopMargin >= t.Lease:
		return DefaultTimings(), fmt.Errorf("ha.stop_margin_seconds (%s) must be below lease_seconds (%s)", t.StopMargin, t.Lease)
	case t.Renew*3 > t.Lease:
		return DefaultTimings(), fmt.Errorf("ha.renew_seconds (%s) times 3 must not exceed lease_seconds (%s)", t.Renew, t.Lease)
	case t.GuardMargin >= t.Lease:
		return DefaultTimings(), fmt.Errorf("ha.guard_margin_seconds (%s) must be below lease_seconds (%s)", t.GuardMargin, t.Lease)
	}
	return t, nil
}

// Clock gives the wall clock and CLOCK_BOOTTIME. A local deadline has passed
// when either clock says so.
type Clock interface {
	Now() time.Time
	Boot() time.Duration
}

// SystemClock is the real clock.
type SystemClock struct{}

func (SystemClock) Now() time.Time { return time.Now() }

func (SystemClock) Boot() time.Duration { return BootTime() }

// BootTime reads CLOCK_BOOTTIME, which is system-wide, so the runner and the
// guard can compare deadlines on it.
func BootTime() time.Duration {
	var ts unix.Timespec
	if err := unix.ClockGettime(unix.CLOCK_BOOTTIME, &ts); err != nil {
		return 0
	}
	return time.Duration(ts.Nano())
}
