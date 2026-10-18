package main

import "sync/atomic"

type atomicUint64 struct{ value atomic.Uint64 }

func (counter *atomicUint64) Add(delta uint64) { counter.value.Add(delta) }
func (counter *atomicUint64) Load() uint64     { return counter.value.Load() }
