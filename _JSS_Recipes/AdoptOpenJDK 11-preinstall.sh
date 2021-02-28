#!/bin/bash

# Remove existing JDK 11 installations

for JAVADIR in "$3"/Library/Java/JavaVirtualMachines/jdk-11*; do
  rm -rf "$JAVADIR"
done

exit 0
