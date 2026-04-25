from dotenv import load_dotenv
load_dotenv()

from openclaw_control.tui.app import OpenClawTUI

if __name__ == "__main__":
    OpenClawTUI().run()