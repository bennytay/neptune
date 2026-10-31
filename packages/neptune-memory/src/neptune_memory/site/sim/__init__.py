"""A simulated Plant 7: a stand-in for a real site that Neptune cannot tell apart.

The plant speaks the site's own protocols (Sparkplug-style PLC data, VDA 5050-style fleet state,
the arm controller's log), writes the site's own records (CMMS work orders), follows the site's
own roster, and takes commands only through those protocols. Neptune's code never imports it.
"""
