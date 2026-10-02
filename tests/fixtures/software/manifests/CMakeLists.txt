cmake_minimum_required(VERSION 3.16)
# The lidar driver's top-level project. ( a parenthesis in a comment
project(lidar_driver
  VERSION 1.8.0
  DESCRIPTION "Driver for a spinning lidar (UDP)"
  LANGUAGES CXX)

add_library(lidar_driver src/driver.cpp)
