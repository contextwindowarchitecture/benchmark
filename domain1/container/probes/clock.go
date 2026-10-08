// Prints the wall clock as Unix seconds, so a run can tell whether a clock shift reaches Go programs.
package main

import (
	"fmt"
	"time"
)

func main() { fmt.Println(time.Now().Unix()) }
