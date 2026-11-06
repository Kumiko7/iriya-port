"""Launcher: python iriya.py [folder-with-roms]"""
import multiprocessing

if __name__ == '__main__':
    multiprocessing.freeze_support()     # the alternative soundtrack decodes in a helper process
    from iriya_engine.app import main
    main()
